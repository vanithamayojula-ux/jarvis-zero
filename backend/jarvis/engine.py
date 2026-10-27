"""Local voice loop: mic → VAD/turn detection → STT → Conversation → TTS → speaker, with barge-in."""
from __future__ import annotations

import asyncio
import collections
import difflib
import re
import statistics
import time
from typing import Callable

import numpy as np

from .audio import Microphone, NullSpeaker, Speaker
from .config import DATA_DIR, Settings
from .conversation import STRICT_YES, Conversation, looks_incomplete
from .gate import ABORT
from .interfaces import LLM, STT, TTS, VAD
from .tools import ToolBox

_SMALL_WORDS = {"of", "the", "a", "an", "to", "by", "in", "on", "and", "for", "my", "i", "it", "is", "at", "me", "so"}


def correct_names(text: str, names: list[str], cutoff: float = 0.78) -> str:
    """'Play Run It Up by Hany Mankind' → '… by Hanumankind': swap a run of words that is spelled almost like one of
    your names (compared letters only, so word breaks don't matter). Only names of 6+ letters, so short everyday
    words are never touched."""
    import difflib
    words = text.split()
    letters = lambda s: re.sub(r"[^a-z]", "", s.lower())  # noqa: E731
    for name in sorted({n for n in names if len(letters(n)) >= 6}, key=lambda n: -len(n)):
        target = letters(name)
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", " ".join(words), re.I):
            continue                                       # already right ("hanuman kind" is not, so it's fixed)
        n_words = len(name.split())
        best = None
        for size in range(1, min(4, len(words)) + 1):
            if n_words > 1 and size != n_words:            # "Run It Up" only replaces three words, never "run it"
                continue
            for i in range(len(words) - size + 1):
                edge = {letters(words[i]), letters(words[i + size - 1])}
                if size > 1 and edge & _SMALL_WORDS:             # "songs of humankind" must keep its "of"
                    continue
                chunk = letters(" ".join(words[i:i + size]))
                if not chunk or abs(len(chunk) - len(target)) > 4:
                    continue
                r = difflib.SequenceMatcher(None, chunk, target).ratio()
                if n_words > 1 and r < 0.85:               # phrase names must match more closely
                    continue
                score = r - 0.08 * abs(len(chunk) - len(target))   # "spot if I" (7 letters) beats "spot if" (6)
                if r >= cutoff and (best is None or score > best[0]):
                    best = (score, i, size)
        if best:
            _, i, size = best
            tail = re.search(r"[^\w']+$", words[i + size - 1])
            words[i:i + size] = [name + (tail.group(0) if tail else "")]
    return " ".join(words)


def is_hallucination(text: str) -> bool:
    """Whisper loops on noise ('okay, okay, okay, …') — drop transcripts that are mostly one repeated word."""
    words = re.findall(r"[a-z']+", text.lower())
    if len(words) >= 6:
        top = max(words.count(w) for w in set(words))
        if top / len(words) > 0.5:
            return True
    # ...or one sentence over and over ("We have a lot of people who are going to … We have a lot of people …").
    if len(words) >= 16:
        grams = [tuple(words[i:i + 4]) for i in range(len(words) - 3)]
        if len(set(grams)) / len(grams) < 0.45:
            return True
    return len(text) > 400 and len(set(words)) < 12


# How Whisper commonly mishears the wake word.
WAKE_ALIASES = {"jarvis": {"travis", "jervis", "jarvas", "javis", "jarvi", "garvis", "charvis", "jarves", "jarviss", "davis"}}


def find_wake_word(text: str, wake: str) -> tuple[bool, str]:
    """Whisper often hears 'Jarvis' as 'Travis', 'Jervis', 'Jarvis's'… Accept close matches in the first few words.

    Returns (found, text with the wake word and any 'hey/ok' before it removed)."""
    words = re.findall(r"[\w']+|\S", text)
    seen = 0
    for i, w in enumerate(words):
        if not re.match(r"\w", w):
            continue
        seen += 1
        if seen > 4:
            break
        bare = re.sub(r"'s$", "", w.lower())
        if bare == wake or bare in WAKE_ALIASES.get(wake, ()) or \
                (len(bare) >= 5 and difflib.SequenceMatcher(None, bare, wake).ratio() >= 0.72):
            rest = "".join(x if not re.match(r"\w", x) else " " + x for x in words[i + 1:]).lstrip(" ,.!?").rstrip()
            return True, rest
    return False, text


CLARIFY = ["Sorry, I didn't catch that. Could you say it again?",
           "I missed that, mind repeating it?",
           "Say that once more? It came through a bit garbled."]


class Engine:
    def __init__(self, settings: Settings, vad: VAD, stt: STT, llm: LLM, tts: TTS, tools: ToolBox | None,
                 mic_factory: Callable[..., Microphone] = Microphone,
                 speaker_factory: Callable[..., Speaker] = Speaker, memory=None):
        self.settings = settings
        self.memory = memory           # what JARVIS remembers about you across conversations (data/memory.json)
        self.vad, self.stt, self.llm, self.tts, self.tools = vad, stt, llm, tts, tools
        self.mic_factory, self.speaker_factory = mic_factory, speaker_factory
        self.loop: asyncio.AbstractEventLoop | None = None
        self.speaker: Speaker | None = None
        self.mic: Microphone | None = None
        self.conversation = Conversation(settings, llm, tools, "local", memory=memory)
        self.conversation.notify = self.emit
        self._yes_ignored = False      # hands-free: a risky "yes" with no wake word is ignored once
        self.state = "idle"
        self.listening = False
        self.ptt = False
        self.latencies: collections.deque[dict] = collections.deque(maxlen=50)
        self._subs: list[asyncio.Queue] = []
        self._frames: asyncio.Queue | None = None
        self._turn: asyncio.Task | None = None
        self._spoken: list[str] = []
        self._utt: list[np.ndarray] = []
        self._preroll: collections.deque = collections.deque(maxlen=20)  # 640 ms kept before speech starts
        self._in_utt = False
        self._voiced = 0
        self._silent_ms = 0.0
        self.turn_model = None         # SmartTurn (set by the server): "finished, or just pausing?"
        self.wake_model = None         # WakeWord (set by the server): hears "hey jarvis" in the raw audio
        self._wake_at = 0.0            # when it last fired
        self._turn_checked_at, self._turn_p = 0.0, 1.0
        self._barge = 0
        self._prefix = ""
        self._prefix_t = 0.0
        self._epoch = 0                # bumped when the user takes the floor (hotkey / PTT)
        self._commit: asyncio.Task | None = None
        self._followup_until = 0.0
        self._clarify_i = 0
        self._clip_base = 0
        self._oneshot = False          # set by the global hotkey: catch one sentence, no wake word
        self._oneshot_task: asyncio.Task | None = None
        self._oneshot_owns_mic = False  # the hotkey opened the mic just for one request
        self._tasks: set[asyncio.Task] = set()
        self.on_io_mode: Callable[[str], None] | None = None   # set by the server: load voice-only models on switch

    # ---------------- chat / voice mode ----------------
    def voice_on(self) -> bool:
        """Voice mode: talk and hear replies. Chat mode: typed in, text out; mic and speaker stay closed."""
        return self.settings.get("io_mode") != "chat"

    def _make_speaker(self):
        if self.voice_on():
            return self.speaker_factory(self.tts.sample_rate, self.settings.get("output_device"))
        return NullSpeaker(self.tts.sample_rate)

    def sync_io_mode(self) -> None:
        """Make the speaker and mic match the io_mode setting (after the dashboard, spidey or a command changed it)."""
        want_voice = self.voice_on()
        if self.speaker is None or want_voice == (not isinstance(self.speaker, NullSpeaker)):
            return
        self.speaker.stop()                     # not interrupt(): a reply in progress carries on, as text or voice
        if not want_voice:
            self.stop_listening()
        self.speaker.close()
        self.speaker = self._make_speaker()
        mode = "voice" if want_voice else "chat"
        self.emit({"type": "io_mode", "mode": mode})
        if self.on_io_mode:
            self.on_io_mode(mode)

    # ---------------- lifecycle ----------------
    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        self._frames = asyncio.Queue(maxsize=400)
        self.speaker = self._make_speaker()
        self._spawn(self._frame_loop())

    async def shutdown(self) -> None:
        self.stop_listening()
        if self._turn:
            self._turn.cancel()
        if self.speaker:
            self.speaker.close()

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)
        return t

    # ---------------- events ----------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subs.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self._subs:
            self._subs.remove(q)

    def emit(self, event: dict) -> None:
        """Thread-safe: may be called from audio threads."""
        if self.loop and not self._in_loop():
            self.loop.call_soon_threadsafe(self.emit, event)
            return
        for q in list(self._subs):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(event)

    def _in_loop(self) -> bool:
        try:
            return asyncio.get_running_loop() is self.loop
        except RuntimeError:
            return False

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.emit({"type": "state", "state": state})

    def status(self) -> dict:
        return {"state": self.state, "listening": self.listening, "ptt": self.ptt,
                "io_mode": "voice" if self.voice_on() else "chat", "mode": self.settings.get("mode"), "activation": self.settings.get("activation"),
                "mic_level": round(self.mic.level, 4) if self.mic else 0.0,
                "looking": getattr(self.tools, "looking_now", lambda: None)(),
                "conversation_id": self.conversation.id, "latency": self.latency_summary(),
                "killed": bool(getattr(getattr(self.tools, "gate", None), "killed", None) and self.tools.gate.killed.is_set())}

    # ---------------- mic control ----------------
    def start_listening(self) -> None:
        self._oneshot_owns_mic = False  # an explicit "start listening" keeps the mic on
        if self.listening:
            return
        if not self.voice_on():
            self.emit({"type": "error", "message": "Chat mode is on, so the mic stays off. Switch to voice mode to talk."})
            return
        self.vad.reset()
        self.mic = self.mic_factory(self._on_frame, self.settings.get("input_device"),
                                    on_error=lambda m: self.emit({"type": "error", "message": m}))
        if hasattr(self.mic, "on_recovered"):
            self.mic.on_recovered = lambda m: (self._voice_log("MIC RECONNECTED"),
                                               self.emit({"type": "mic", "listening": True, "info": m}))
        self.mic.start()
        self.listening = True
        self.emit({"type": "mic", "listening": True})

    def stop_listening(self) -> None:
        if self.mic:
            self.mic.stop()
            self.mic = None
        self.listening = False
        self.ptt = False
        self._reset_utt()
        self.emit({"type": "mic", "listening": False})

    def listen_once(self) -> None:
        """Global hotkey: stop talking, beep, and catch the next sentence (ends by itself when you stop speaking)."""
        self.interrupt()
        if not self.voice_on():
            self.emit({"type": "open_chat"})   # chat mode: the floating bot opens its typing bar instead
            return
        self._take_floor()
        if not self.listening:
            self.start_listening()
            # Mic was switched off in hands-free mode: open it for this one request only, never silently
            # leave wake-word listening running afterwards.
            self._oneshot_owns_mic = self.settings.get("mode") == "hands_free"
        self._reset_utt()
        self.vad.reset()
        self._oneshot = True
        self._set_state("listening")
        self.emit({"type": "oneshot", "listening": True})
        if self.speaker:
            t = np.arange(int(self.tts.sample_rate * 0.12)) / self.tts.sample_rate
            beep = (np.sin(2 * np.pi * 880 * t) * np.hanning(len(t)) * 6000).astype(np.int16)
            self.speaker.play(beep)
        if self._oneshot_task and not self._oneshot_task.done():
            self._oneshot_task.cancel()
        self._oneshot_task = self._spawn(self._oneshot_timeout())
        self._spawn(asyncio.to_thread(self._pause_media_for_listening))

    def _pause_media_for_listening(self) -> None:
        """The mic can't hear you over a playing video, so pause it while you talk (say "play" to resume)."""
        edge = getattr(self.tools, "edge", None) if self.tools else None
        try:
            if edge is not None and edge.is_playing():
                edge.media("pause")
                self._voice_log("PAUSED video in Edge for hotkey request")
                self.emit({"type": "media", "paused_for_hotkey": True})
        except Exception:
            pass  # never let browser trouble break listening

    async def _oneshot_timeout(self, seconds: float = 8.0) -> None:
        await asyncio.sleep(seconds)
        if self._oneshot and not self._in_utt:  # pressed the hotkey but said nothing
            self._oneshot = False
            self._set_state("idle")
            self.emit({"type": "oneshot", "listening": False})
            self._release_oneshot_mic()

    def _release_oneshot_mic(self) -> None:
        if self._oneshot_owns_mic and not self._oneshot:
            self._oneshot_owns_mic = False
            self.stop_listening()

    async def _release_mic_after(self, handling: asyncio.Task) -> None:
        """After a hotkey request (mic was off): wait for the reply to finish, then turn the mic back off."""
        try:
            await handling
        except Exception:
            pass
        while (self._turn and not self._turn.done()) or (self._commit and not self._commit.done()):
            await asyncio.sleep(0.1)
        self._release_oneshot_mic()

    def _finish_utterance(self, pcm: np.ndarray, oneshot: bool) -> None:
        task = self._spawn(self._handle_utterance(pcm, time.perf_counter(), require_wake=not oneshot))
        if oneshot and self._oneshot_owns_mic:
            self._spawn(self._release_mic_after(task))

    def _take_floor(self) -> None:
        """Hotkey / push-to-talk pressed: the user starts a NEW request. Anything still being transcribed from
        before, or a held half-sentence, must not turn into a reply on top of it."""
        self._epoch += 1
        if self._commit and not self._commit.done():
            self._commit.cancel()
        self._prefix = ""

    def set_ptt(self, down: bool) -> None:
        """Push-to-talk: pressing interrupts JARVIS; releasing sends what you said."""
        if down and not self.ptt:
            if not self.listening:
                self.start_listening()
            self.interrupt()
            self._take_floor()
            self._reset_utt()
            self.ptt = True
            self._set_state("listening")
        elif not down and self.ptt:
            self.ptt = False
            pcm = np.concatenate(self._utt) if self._utt else np.zeros(0, np.int16)
            self._reset_utt()
            if len(pcm) > 16000 * 0.25:
                self._spawn(self._handle_utterance(pcm, time.perf_counter(), require_wake=False))
            else:
                self._set_state("idle")

    def _on_frame(self, frame: np.ndarray) -> None:  # audio thread
        if self.loop and self._frames is not None:
            self.loop.call_soon_threadsafe(self._push_frame, frame)

    def _push_frame(self, frame: np.ndarray) -> None:
        if self._frames.full():
            self._frames.get_nowait()
        self._frames.put_nowait(frame)

    def _reset_utt(self) -> None:
        self._utt, self._in_utt, self._voiced, self._silent_ms = [], False, 0, 0.0
        self._preroll.clear()

    # ---------------- turn detection ----------------
    async def _frame_loop(self) -> None:
        while True:
            try:
                frame = await asyncio.wait_for(self._frames.get(), 1.0)
            except asyncio.TimeoutError:
                self._on_stall()
                continue
            if not self.listening:
                continue
            try:
                self._process_frame(frame)
            except Exception as exc:  # one bad frame (VAD error, odd shape) must not kill listening for good
                self._voice_log(f"ERROR in frame loop: {exc!r}")
                self._reset_utt()

    def _on_stall(self) -> None:
        """No audio for a second (mic unplugged / driver stalled; the Microphone watchdog reconnects it).
        Don't leave a half-heard sentence hanging in "listening": send what we have, or drop it."""
        if not (self.listening and self._in_utt):
            return
        frame_ms = 512 / 16000 * 1000
        voiced_s = len(self._utt) * frame_ms / 1000
        pcm = np.concatenate(self._utt) if self._utt else np.zeros(0, np.int16)
        self._reset_utt()
        self.vad.reset()
        self._voice_log(f"MIC STALLED mid-utterance ({voiced_s:.1f}s captured)")
        oneshot, self._oneshot = self._oneshot, False
        if voiced_s >= 0.4:
            self._set_state("thinking")
            self._finish_utterance(pcm, oneshot)
        else:
            self._set_state("idle")
            if oneshot:
                self._release_oneshot_mic()
        if oneshot:
            self.emit({"type": "oneshot", "listening": False})

    def _process_frame(self, frame: np.ndarray) -> None:
        frame_ms = 512 / 16000 * 1000
        if self.settings.get("mode") == "push_to_talk" and not self._oneshot:
            if self.ptt:
                self._utt.append(frame)
            return
        p = self.vad.prob(frame)
        speech = p >= self.settings.get("vad_threshold")
        if self._wake_active():
            try:
                score = self.wake_model.feed(frame)
            except Exception as exc:
                self._voice_log(f"wake word failed: {exc!r}")
                score = 0.0
            if score >= self.wake_model.threshold and time.monotonic() - self._wake_at > 1.5:
                self._wake_at = time.monotonic()
                self._voice_log(f"WAKE WORD ({score:.2f})")
                self.emit({"type": "wake", "score": round(score, 2)})
                if not self._in_utt and self.state == "idle":
                    self._set_state("listening")      # spidey's ring lights up: it heard you
        # While JARVIS talks, only a clearly louder voice counts (laptop mic hears the speaker too).
        if self.speaker and self.speaker.busy:
            mic_level = float(np.sqrt(np.mean((frame.astype(np.float32) / 32768.0) ** 2)))
            expected_echo = self.settings.get("echo_coupling") * self.speaker.level
            loud = mic_level > max(expected_echo * self.settings.get("echo_margin"),
                                   self.settings.get("noise_floor") * 2, 0.004)
            self._barge = self._barge + 1 if (speech and loud and self.settings.get("barge_in")) else 0
            if self._barge >= 6:  # ~190 ms of sustained speech
                self.interrupt()
                self._barge = 0
                self._begin_utt(list(self._preroll)[-8:])
            else:
                # Mid-sentence when a beep/reminder started: keep the user's words instead of dropping them.
                (self._utt if self._in_utt else self._preroll).append(frame)
                return
        if not self._in_utt:
            self._preroll.append(frame)
            self._voiced = self._voiced + 1 if speech else 0
            if self._voiced >= 3:
                self._begin_utt(list(self._preroll))
            return
        self._utt.append(frame)
        if speech:
            self._silent_ms, self._turn_checked_at = 0.0, 0.0
        else:
            self._silent_ms += frame_ms
        total_s = len(self._utt) * frame_ms / 1000
        done = self._silent_ms >= self.settings.get("silence_ms")
        if done and self.turn_model and total_s < self.settings.get("max_utterance_s"):
            # Smart Turn only ever adds patience: at the usual pause it gets a vote, and if this sounds like a
            # mid-sentence pause ("open WhatsApp and message… Rahul"), keep listening up to smart_turn_max_ms.
            if self._silent_ms < self.settings.get("smart_turn_max_ms"):
                if self._silent_ms - self._turn_checked_at >= 300:     # one check per ~300 ms of quiet
                    self._turn_checked_at = self._silent_ms
                    try:
                        p = self.turn_model.prob(np.concatenate(self._utt))
                    except Exception as exc:
                        self._voice_log(f"smart turn failed: {exc!r}")
                        p = 1.0
                    self._turn_p = p
                done = self._turn_p >= 0.5
        if done or total_s >= self.settings.get("max_utterance_s"):
            if self.turn_model and self._silent_ms > self.settings.get("silence_ms") + 200:
                self._voice_log(f"SMART TURN waited {self._silent_ms / 1000:.1f}s (p(done)={self._turn_p:.2f})")
            self._turn_checked_at, self._turn_p = 0.0, 1.0
            pcm = np.concatenate(self._utt)
            self._reset_utt()
            self.vad.reset()
            self._set_state("thinking")
            oneshot, self._oneshot = self._oneshot, False  # a hotkey request needs no wake word
            self._finish_utterance(pcm, oneshot)

    def _wake_active(self) -> bool:
        return bool(self.wake_model) and self.settings.get("mode") == "hands_free" and \
            self.settings.get("activation") == "wake_word" and self.settings.get("wake_word").lower() == "jarvis"

    def _begin_utt(self, frames: list[np.ndarray]) -> None:
        self._in_utt = True
        self._utt = list(frames)
        self._silent_ms = 0.0
        self._turn_checked_at, self._turn_p = 0.0, 1.0
        if self._commit and not self._commit.done():
            self._commit.cancel()  # they kept talking: hold the turn
        self._set_state("listening")

    # ---------------- utterance → reply ----------------
    async def _handle_utterance(self, pcm: np.ndarray, t_end: float, require_wake: bool) -> None:
        try:
            await self._handle_utterance_inner(pcm, t_end, require_wake)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken STT model must not leave the dashboard stuck on "thinking"
            self._voice_log(f"ERROR handling speech: {exc!r}")
            self.emit({"type": "error", "message": f"Speech recognition failed: {exc}"})
            self._commit_held(t_end, 0.0)
            if self.state == "thinking":
                self._set_state("idle")

    def _commit_held(self, t_end: float, stt_s: float) -> bool:
        """A held, unfinished sentence whose continuation turned out to be noise: answer what we have now."""
        if not self._prefix or (self._commit and not self._commit.done()):
            return False
        text, self._prefix = self._prefix, ""
        self._start_turn(text, t_end, stt_s)
        return True

    async def _handle_utterance_inner(self, pcm: np.ndarray, t_end: float, require_wake: bool) -> None:
        self._set_state("thinking")
        epoch = self._epoch
        hint = self._stt_hint()
        tr = await asyncio.to_thread(self.stt.transcribe, pcm, hint)
        if epoch != self._epoch:  # hotkey/PTT pressed while this was transcribing: the user moved on
            self._voice_log(f"SUPERSEDED by hotkey/push-to-talk: {tr.text.strip()[:80]!r}")
            return
        text = correct_names(tr.text.strip(), self.settings.get("name_hints") or [])
        stt_s = time.perf_counter() - t_end
        # The user said "Jarvis, so I was thinking and…", paused, and is now finishing the sentence:
        # the continuation needs no wake word (it is still the same request).
        if self._prefix and time.monotonic() - self._prefix_t > 15:
            self._prefix = ""  # a stale half-sentence must never get glued onto a new request
        holding = bool(self._prefix)
        if not text or re.fullmatch(r"\W*(you|thank you|thanks for watching)\W*", text, re.I) or is_hallucination(text):
            self._voice_log(f"DROPPED noise/hallucination ({len(pcm) / 16000:.1f}s audio, peak {np.abs(pcm).max() / 32768:.3f}): {text[:80]!r}")
            if not self._commit_held(t_end, stt_s):
                self._set_state("idle")
            return
        self.emit({"type": "heard", "text": text, "confidence": round(tr.confidence, 2)})
        found = False
        continuing = holding and tr.confidence >= self.settings.get("followup_min_confidence")
        if continuing:
            found, text_wo = find_wake_word(text, self.settings.get("wake_word").lower())
            text, found = (text_wo if found else text) or text, True
        elif require_wake and self.settings.get("activation") == "wake_word":
            wake = self.settings.get("wake_word").lower()
            # While a video/music site JARVIS opened may be playing, the speakers talk too: always require the wake word.
            media_playing = await asyncio.to_thread(self._media_playing)
            typing = getattr(self.conversation, "typing", None) is not None   # typing mode: keep taking dictation
            in_followup = (time.monotonic() < self._followup_until or typing) and not media_playing
            found, text_wo = find_wake_word(text, wake)
            if self._wake_active():
                # The audio model decides, not the transcript: "Travis"/"Jervis" no longer count. Backup: the exact
                # word "Jarvis" opening a clearly heard sentence (the model is trained on "hey jarvis").
                woke = time.monotonic() - self._wake_at < 12
                exact = re.match(r"^\W*(?:(?:hey|hi|ok|okay|yo)\W+)?jarvis\b", text, re.I) and tr.confidence >= 0.6
                found = bool(woke or exact)
                if woke:
                    self._wake_at = 0.0               # one "hey jarvis" wakes one request
                if found:
                    text_wo = re.sub(r"^\W*(?:(?:hey|hi|ok|okay|yo)\W+)?(?:jarvis|travis|jervis|jarvi)\b\W*", "", text,
                                     flags=re.I).strip()
            # Without the wake word, only accept clear speech: videos and music from the speakers come
            # through as low-confidence fragments ("God bless you", "comment in the comments").
            if not found and (not in_followup or tr.confidence < self.settings.get("followup_min_confidence")):
                if in_followup:
                    why = f"unclear ({tr.confidence:.2f}) without wake word"
                else:
                    why = "no wake word (media playing)" if media_playing else "no wake word"
                self._voice_log(f"IGNORED ({why}): {text}")
                self.emit({"type": "ignored", "text": text, "reason": f"{why} — start with “{wake.capitalize()}”"})
                if not self._commit_held(t_end, stt_s):
                    self._set_state("idle")
                return
            text = (text_wo if found else text) or "Hello."
        self._voice_log(f"HEARD ({tr.confidence:.2f}, {len(pcm) / 16000:.1f}s audio, stt {stt_s:.2f}s): {text}")
        if tr.confidence < 0.3 and len(text.split()) <= 3:
            if self._commit_held(t_end, stt_s):  # garbled tail after an unfinished sentence: answer the sentence
                return
            if require_wake and not found:  # hands-free background noise: stay quiet instead of nagging
                self._set_state("idle")
                return
            await self._speak_fixed(CLARIFY[self._clarify_i % len(CLARIFY)])
            self._clarify_i += 1
            return
        text = (self._prefix + " " + text).strip()
        self._prefix = ""
        pending = self.conversation.pending
        if require_wake and self.settings.get("activation") == "wake_word" and not found and pending and pending.risk == "high" and STRICT_YES.match(text):
            # hands-free: a spoken "yes" to a send/buy/press without the wake word could be a TV. Ignore it once.
            if not self._yes_ignored:
                self._yes_ignored = True
                self._voice_log("IGNORED once (risky yes without wake word)")
                self.emit({"type": "ignored", "text": text, "reason": "say “Jarvis, yes” to confirm"})
                self._set_state("idle")
                return
        self._yes_ignored = False
        if looks_incomplete(text) and require_wake:
            # Sounds unfinished ("so I was thinking and…"): give them a moment before answering.
            self._prefix, self._prefix_t = text, time.monotonic()
            self._commit =self._spawn(self._commit_after(t_end, stt_s))
            return
        self._start_turn(text, t_end, stt_s)

    async def _commit_after(self, t_end: float, stt_s: float) -> None:
        await asyncio.sleep(self.settings.get("hold_ms") / 1000)
        text, self._prefix = self._prefix, ""
        if text:
            self._start_turn(text, t_end, stt_s)

    def submit_text(self, text: str) -> None:
        """Typed input from the dashboard (also used by the local assistant test)."""
        self.interrupt()
        self._start_turn(text, time.perf_counter(), 0.0)

    def _start_turn(self, text: str, t_end: float, stt_s: float) -> None:
        ABORT.clear()                  # a new turn starts clean (the gate's latch stays until "resume")
        if self._turn and not self._turn.done():
            self._turn.cancel()
        self.emit({"type": "transcript", "role": "user", "text": text})
        self.conversation.spoken_input = stt_s > 0     # typed text is taken word for word ("type …" keeps its full stop)
        self._turn = self._spawn(self._run_turn(text, t_end, stt_s))

    async def _run_turn(self, text: str, t_end: float, stt_s: float) -> None:
        self._set_state("thinking")
        self._spoken = []
        self._clip_base = self.speaker.clips_started
        first_audio = None
        tts_total = 0.0
        try:
            async for sentence in self.conversation.reply(text):
                self.sync_io_mode()                    # "switch to chat mode" takes effect from this sentence on
                if not self.voice_on():                # chat mode: text only, nothing synthesized
                    if first_audio is None:
                        first_audio = time.perf_counter() - t_end
                    self._spoken.append(sentence)
                    self.emit({"type": "transcript", "role": "assistant", "text": sentence})
                    continue
                t = time.perf_counter()
                clip = await asyncio.to_thread(self.tts.synthesize, sentence)
                tts_total += time.perf_counter() - t
                if first_audio is None:
                    first_audio = time.perf_counter() - t_end
                    self._set_state("speaking")
                    self._followup_until = time.monotonic() + 120.0  # while talking, no wake word needed
                self.speaker.play(clip)
                self._spoken.append(sentence)
                self.emit({"type": "transcript", "role": "assistant", "text": sentence})
            m = self.conversation.metrics
            record = {"stt_s": round(stt_s, 3),
                      "llm_first_token_s": round(m.get("llm_first_token") or 0, 3),
                      "first_sentence_s": round(m.get("first_sentence") or 0, 3),
                      "tts_s": round(tts_total, 3),
                      "first_audio_s": round(first_audio or 0, 3),
                      "silence_wait_s": self.settings.get("silence_ms") / 1000 if stt_s else 0.0}
            self.latencies.append(record)
            self.emit({"type": "latency", **record})
            pending = self.conversation.pending
            self.emit({"type": "confirm", "prompt": pending.confirm_prompt if pending else None,
                       "choices": [name for name, _ in pending.choices] if pending and pending.choices else None})
            await asyncio.to_thread(self.speaker.wait_idle, 60)
            self._followup_until = time.monotonic() + self.settings.get("followup_s")  # follow-ups without the wake word
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.emit({"type": "error", "message": f"Reply failed: {exc}"})
            await self._speak_fixed("Sorry, something went wrong on my end. Try that again?")
        finally:
            if self.state != "listening":
                self._set_state("idle")

    async def _speak_fixed(self, text: str) -> None:
        self.emit({"type": "transcript", "role": "assistant", "text": text})
        if not self.voice_on():
            return
        clip = await asyncio.to_thread(self.tts.synthesize, text)
        self._set_state("speaking")
        self.speaker.play(clip)
        await asyncio.to_thread(self.speaker.wait_idle, 30)
        if self.state == "speaking":  # the user may already be talking again: don't flip that to idle
            self._set_state("idle")

    def interrupt(self) -> None:
        """Stop talking now. Keeps only the sentences the user actually heard in memory."""
        # Only a reply in progress counts: the hotkey beep, a reminder or a "didn't catch that" line playing
        # must not rewrite the previous (fully heard) reply in memory.
        was_talking = bool(self._turn and not self._turn.done())
        if self.speaker:
            self.speaker.stop()
        if self._turn and not self._turn.done():
            self._turn.cancel()
        if was_talking:
            started = (self.speaker.clips_started - self._clip_base if self.speaker else 0) if self.voice_on() \
                else len(self._spoken)              # chat mode: every line shown was read
            heard = self._spoken[:max(0, started)]
            hist = self.conversation.history
            if hist and hist[-1]["role"] == "assistant":  # nothing generated yet → leave the earlier reply alone
                self.conversation.mark_interrupted(heard)
            self._followup_until = time.monotonic() + self.settings.get("followup_s")
            self.emit({"type": "interrupted"})

    def kill(self) -> None:
        """The kill switch: every click and key stops now, and stays stopped until the person says "resume".
        The gate's latch is set first (from whichever thread this is), then everything in flight is cut off."""
        gate = getattr(self.tools, "gate", None)
        if gate:
            gate.kill()
        self.interrupt()
        self.conversation.pending = None
        edge = getattr(self.tools, "edge", None)
        if edge is not None:
            try:
                edge.auto_scroll(False)
                edge.banner(None)
            except Exception:
                pass
        self.emit({"type": "kill"})

    def emergency_stop(self) -> None:
        self.kill()
        self.interrupt()
        self.stop_listening()
        self._set_state("idle")
        self.emit({"type": "emergency_stop"})

    def new_conversation(self) -> None:
        self.interrupt()
        if self.memory:
            self.memory.drop_recent()  # a new conversation starts a fresh thread; the facts about you stay
        self.conversation = Conversation(self.settings, self.llm, self.tools, "local", memory=self.memory)
        self.conversation.notify = self.emit
        self.emit({"type": "conversation", "id": self.conversation.id})

    def latency_summary(self) -> dict:
        # Spoken turns only: typed messages skip speech recognition and would flatter the numbers.
        voice = [r for r in self.latencies if r["stt_s"] > 0]
        if not voice:
            return {}
        keys = ["stt_s", "llm_first_token_s", "first_sentence_s", "first_audio_s"]
        return {k: round(statistics.median(r[k] for r in voice), 3) for k in keys} | {"turns": len(voice)}

    def _media_playing(self) -> bool:
        """True while a video/music may be coming out of the speakers (so speech without the wake word is ignored)."""
        if not self.tools:
            return False
        edge = getattr(self.tools, "edge", None)
        if edge is not None and edge.is_playing():
            return True
        # Media opened in another browser can't be inspected: assume it may play for 30 minutes.
        return time.time() - getattr(self.tools, "media_opened_at", 0) < 1800

    def _stt_hint(self) -> str:
        """Prime Whisper with the words this user actually says (wake word, their apps, web apps)."""
        from .tools import WEB_APPS
        now = time.monotonic()
        if getattr(self, "_hint_cache", None) and now - self._hint_cache[0] < 60:
            return self._hint_cache[1]
        apps = [a.title() if a.islower() else a for a in (self.tools.app_names() if self.tools else [])]
        web = [w["name"] for w in WEB_APPS.values()]
        wake = self.settings.get("wake_word").capitalize()
        # Your own names first (people, artists, songs: "Hanumankind", "Rahul"): they're what Whisper gets wrong,
        # and the hint is cut at ~110 words, so they must not be the part that falls off the end.
        names = [str(n).strip() for n in (self.settings.get("name_hints") or []) if str(n).strip()]
        hint = ((", ".join(dict.fromkeys(names)) + ". ") if names else "") + \
            (f"{wake}, open WhatsApp in Chrome. {wake}, open YouTube. Open the DSA folder. "
             + ", ".join(dict.fromkeys(web + apps)) + ".")
        hint = " ".join(hint.split()[:110])  # Whisper's prompt window is ~224 tokens
        self._hint_cache = (now, hint)
        return hint

    def _voice_log(self, line: str) -> None:
        if not self.settings.get("voice_log"):
            return
        try:
            path = DATA_DIR / "voice.log"
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > 1_000_000:
                path.replace(path.with_suffix(".old.log"))
            with path.open("a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%H:%M:%S')} [{self.settings.get('mode')}] {line}\n")
        except OSError:
            pass

    def say_now(self, text: str) -> None:
        """Speak an unprompted line (e.g. a reminder) once JARVIS is not mid-reply or mid-sentence from the user."""
        self._spawn(self._say_when_free(text))

    async def _say_when_free(self, text: str, max_wait: float = 90.0) -> None:
        end = time.monotonic() + max_wait
        while time.monotonic() < end and ((self._turn and not self._turn.done()) or self._in_utt or
                                          self.state in ("thinking", "listening") or
                                          (self.speaker and self.speaker.busy)):
            await asyncio.sleep(0.2)
        await self._speak_fixed(text)
