"""Call orchestration: lifecycle, AI conversation per call, audio in/out, handoff, emergency stop."""
from __future__ import annotations

import asyncio
import re
import time
from typing import Callable

import numpy as np

from ..config import Settings
from ..conversation import Conversation, system_prompt
from ..interfaces import LLM, STT, TTS, VAD
from .base import CallSession, CallState, TelephonyProvider
from .codec import from_phone, to_phone
from .gate import CostSafetyGate, GateDecision
from .mock import MockProvider
from .qr import qr_svg
from .sip_lan import DEFAULT_ALLOWED, HOTSPOT_PRESETS, Allowlist, SipLanProvider, lan_addresses


class OwnerPhoneConversation(Conversation):
    """Owner mode: a phone call that may use the local toolset. Same routing and confirmations as the laptop
    conversation; the system prompt is the local one (tools allowed) plus a note that this is a phone call."""

    def messages(self) -> list[dict]:
        msgs = super().messages()
        if msgs and msgs[0]["role"] == "system":
            msgs[0] = {"role": "system", "content": system_prompt(self.settings, "local") +
                       "\n- This conversation is a phone call from the owner's own phone. Keep replies short, and ask "
                       "before anything risky exactly as you would on the laptop."}
        return msgs


class _CallRuntime:
    def __init__(self, call: CallSession, conversation: Conversation, vad: VAD):
        self.call, self.conversation, self.vad = call, conversation, vad
        self.pending = np.zeros(0, np.int16)
        self.utt: list[np.ndarray] = []
        self.in_utt, self.voiced, self.silent_ms = False, 0, 0.0
        self.turn: asyncio.Task | None = None
        self.speaking_until = 0.0
        self.latencies: list[float] = []
        self.reply_spoken: list[tuple[str, float]] = []  # (sentence, when its audio starts) for barge-in memory


class TelephonyManager:
    def __init__(self, settings: Settings, llm: LLM, stt: STT, tts: TTS, vad_factory: Callable[[], VAD],
                 emit: Callable[[dict], None], tools=None):
        self.settings, self.llm, self.stt, self.tts = settings, llm, stt, tts
        self.tools = tools          # local ToolBox; only owner-mode calls ever get it
        self.vad_factory, self.emit = vad_factory, emit
        self.gate = CostSafetyGate()
        self.providers: dict[str, TelephonyProvider] = {}
        self.calls: dict[str, _CallRuntime] = {}
        self.history: list[dict] = []
        self._add(MockProvider())

    def _add(self, provider: TelephonyProvider) -> None:
        provider.on_incoming = self._on_incoming
        provider.on_audio = self._on_audio
        provider.on_state = self._on_state
        self.providers[provider.name] = provider

    async def start(self) -> None:
        await self.providers["mock"].start()
        if self.settings.get("telephony")["sip_lan_enabled"]:
            await self.enable_sip_lan()

    async def stop(self) -> None:
        for rt in list(self.calls.values()):
            await self.hangup(rt.call.id)
        for p in self.providers.values():
            await p.stop()

    async def enable_sip_lan(self) -> dict:
        tel = self.settings.get("telephony")
        if "sip_lan" not in self.providers:
            lo, hi = int(tel.get("rtp_port_min") or 0), int(tel.get("rtp_port_max") or 0)
            p = SipLanProvider(tel["sip_bind_host"], tel["sip_port"],
                               allowed_ips=lambda: self.settings.get("telephony").get("sip_allowed_ips") or DEFAULT_ALLOWED,
                               rtp_ports=(lo, hi) if lo and hi > lo else None)
            await p.start()
            self._add(p)
        return self.providers["sip_lan"].status()

    async def disable_sip_lan(self) -> None:
        p = self.providers.pop("sip_lan", None)
        if p:
            await p.stop()

    # ---------------- status ----------------
    def status(self) -> dict:
        tel = self.settings.get("telephony")
        return {
            "providers": [p.status() for p in self.providers.values()],
            "real_calls_enabled": tel["real_calls_enabled"],
            "pstn_available": False,
            "pstn_reason": "No verified zero-cost route to ordinary phone numbers exists. Real phone calls stay disabled.",
            "emergency_locked": self.gate.locked,
            "active_calls": [rt.call.to_dict() for rt in self.calls.values()],
            "recent_calls": self.history[-10:],
        }

    def phone_setup(self) -> dict:
        """Everything the dashboard's "Call JARVIS from your phone" card needs (addresses, QR codes, allowlist)."""
        import ipaddress
        tel = self.settings.get("telephony")
        p = self.providers.get("sip_lan")
        port = p.port if p else tel["sip_port"]
        allowed = Allowlist(tel.get("sip_allowed_ips") or DEFAULT_ALLOWED)
        addresses = []
        for a in lan_addresses():
            net = ipaddress.ip_network(a["network"])
            uri = f"sip:jarvis@{a['ip']}:{port}"
            hotspot = a["ip"].startswith("192.168.137.")
            addresses.append({
                **a, "uri": uri, "qr_svg": qr_svg(uri), "hotspot": hotspot,
                "label": "Windows Mobile Hotspot" if hotspot else a["iface"],
                "callers_allowed": any(n.version == 4 and net.overlaps(n) for n in allowed.networks[1:])})
        status = p.status() if p else {}
        return {
            "enabled": bool(tel["sip_lan_enabled"]), "listening": bool(status.get("listening")),
            "tcp": bool(status.get("tcp")), "port": port, "addresses": addresses,
            "hotspot_active": any(a["hotspot"] for a in addresses),
            "allowed": ["127.0.0.0/8 (this PC)"] + allowed.entries, "invalid_allowed": allowed.invalid,
            "presets": HOTSPOT_PRESETS, "owner_ips": tel.get("owner_ips") or [],
            "owner_mode_available": self.tools is not None,
            "rtp_ports": [tel.get("rtp_port_min"), tel.get("rtp_port_max")],
            "ignored_sources": status.get("ignored_sources", {}), "registrations": status.get("registrations", []),
            "error": status.get("error", ""),
        }

    # ---------------- lifecycle ----------------
    async def place_call(self, provider_name: str, destination: str, consent: bool) -> tuple[GateDecision, dict | None]:
        provider = self.providers.get(provider_name)
        if not provider:
            return GateDecision(False, f"Provider '{provider_name}' is not available."), None
        decision = self.gate.check_outbound(provider, destination, self.settings.get("telephony"), consent)
        call = CallSession(direction="outbound", remote=destination, provider=provider.name, kind=provider.kind)
        if not decision.allowed:
            call.state, call.error, call.ended_at = CallState.BLOCKED, decision.reason, time.time()
            self._archive(call)
            self.emit({"type": "call", "call": call.to_dict()})
            return decision, call.to_dict()
        self._runtime(call)
        asyncio.create_task(self._dial(provider, call))
        return decision, call.to_dict()

    async def _dial(self, provider: TelephonyProvider, call: CallSession) -> None:
        try:
            await provider.dial(call)
        except Exception as exc:
            call.state, call.error = CallState.FAILED, str(exc)
            self._on_state(call)

    def is_owner(self, ip: str) -> bool:
        owners = self.settings.get("telephony").get("owner_ips") or []
        return bool(ip and owners and self.tools is not None and ip in Allowlist(owners, loopback=False))

    def _runtime(self, call: CallSession) -> _CallRuntime:
        call.owner = call.direction == "inbound" and self.is_owner(call.peer_ip)
        if call.owner:
            conv: Conversation = OwnerPhoneConversation(self.settings, self.llm, tools=self.tools, channel="phone")
        else:
            conv = Conversation(self.settings, self.llm, tools=None, channel="phone")
        rt = _CallRuntime(call, conv, self.vad_factory())
        self.calls[call.id] = rt
        return rt

    def _on_incoming(self, call: CallSession) -> None:
        if self.gate.locked:
            call.error = "Rejected: emergency stop is active (reset it to take calls again)."
            asyncio.ensure_future(self.providers[call.provider].hangup(call))
            return
        self._runtime(call)
        self.emit({"type": "call", "call": call.to_dict()})
        asyncio.ensure_future(self._auto_answer(call))

    async def _auto_answer(self, call: CallSession) -> None:
        await asyncio.sleep(0.8)  # one short ring
        if call.id in self.calls and call.state == CallState.RINGING:
            await self.providers[call.provider].answer(call)
            if call.provider == "mock":
                call.answered_at = time.time()

    def _on_state(self, call: CallSession) -> None:
        if call.state == CallState.ACTIVE and not call.answered_at:
            call.answered_at = time.time()
        self.emit({"type": "call", "call": call.to_dict()})
        rt = self.calls.get(call.id)
        if call.state == CallState.ACTIVE and rt and not rt.call.transcript:
            asyncio.ensure_future(self._greet(rt))
        if call.state in (CallState.ENDED, CallState.FAILED):
            call.ended_at = call.ended_at or time.time()
            rt = self.calls.pop(call.id, None)
            if rt and rt.turn:
                rt.turn.cancel()
            self._archive(call)

    def _archive(self, call: CallSession) -> None:
        self.history.append(call.to_dict())
        self.history = self.history[-50:]

    async def _greet(self, rt: _CallRuntime) -> None:
        user = self.settings.get("user_name") or "the owner of this computer"
        notice = " I keep a text transcript of calls on this computer." if self.settings.get("save_conversations") else ""
        text = f"Hi, this is JARVIS, an AI assistant answering for {user}.{notice} How can I help?"
        rt.conversation.history.append({"role": "assistant", "content": text})
        await self._speak(rt, text)

    async def hangup(self, call_id: str) -> None:
        rt = self.calls.get(call_id)
        if rt:
            await self.providers[rt.call.provider].hangup(rt.call)

    async def handoff(self, call_id: str) -> None:
        """Pause the AI so a human can take over (operator replies are spoken into the call)."""
        rt = self.calls.get(call_id)
        if not rt:
            return
        rt.call.ai_active = False
        rt.call.state = CallState.HANDOFF
        self._on_state(rt.call)
        await self._speak(rt, "One moment, I'm handing you over to a person.")

    async def resume_ai(self, call_id: str) -> None:
        rt = self.calls.get(call_id)
        if rt:
            rt.call.ai_active = True
            rt.call.state = CallState.ACTIVE
            self._on_state(rt.call)

    async def operator_say(self, call_id: str, text: str) -> None:
        rt = self.calls.get(call_id)
        if rt:
            await self._speak(rt, text, role="operator")

    async def emergency_stop(self) -> None:
        self.gate.emergency_lock()
        for rt in list(self.calls.values()):
            await self.hangup(rt.call.id)
        self.emit({"type": "telephony", "status": self.status()})

    # ---------------- audio path ----------------
    def _on_audio(self, call: CallSession, payload: bytes) -> None:
        rt = self.calls.get(call.id)
        if not rt or call.state not in (CallState.ACTIVE, CallState.HANDOFF):
            return
        pcm = from_phone(payload, call.codec, 16000)
        rt.pending = np.concatenate([rt.pending, pcm])
        while len(rt.pending) >= 512:
            frame, rt.pending = rt.pending[:512], rt.pending[512:]
            self._frame(rt, frame)

    def _frame(self, rt: _CallRuntime, frame: np.ndarray) -> None:
        speech = rt.vad.prob(frame) >= self.settings.get("vad_threshold")
        if not rt.in_utt:
            rt.voiced = rt.voiced + 1 if speech else 0
            if rt.voiced >= 3:
                rt.in_utt, rt.utt, rt.silent_ms = True, [frame], 0.0
                if time.monotonic() < rt.speaking_until and self.settings.get("barge_in"):
                    self._interrupt(rt)
            return
        rt.utt.append(frame)
        rt.silent_ms = 0.0 if speech else rt.silent_ms + 32.0
        if rt.silent_ms >= self.settings.get("silence_ms") or len(rt.utt) * 0.032 > 20:
            pcm = np.concatenate(rt.utt)
            rt.in_utt, rt.utt, rt.voiced = False, [], 0
            rt.vad.reset()
            asyncio.ensure_future(self._handle_speech(rt, pcm, time.perf_counter()))

    def _interrupt(self, rt: _CallRuntime) -> None:
        if rt.turn and not rt.turn.done():
            rt.turn.cancel()
        p = self.providers.get(rt.call.provider)
        if hasattr(p, "flush_audio"):
            p.flush_audio(rt.call)
        rt.speaking_until = 0.0
        # Remember only the sentences whose audio had started reaching the caller.
        if rt.reply_spoken:
            now = time.monotonic()
            heard = [text for text, start in rt.reply_spoken if start <= now]
            hist = rt.conversation.history
            if len(heard) < len(rt.reply_spoken) and hist and hist[-1]["role"] == "assistant":
                rt.conversation.mark_interrupted(heard)
            rt.reply_spoken = []

    async def _handle_speech(self, rt: _CallRuntime, pcm: np.ndarray, t_end: float) -> None:
        from ..engine import is_hallucination
        try:
            tr = await asyncio.to_thread(self.stt.transcribe, pcm, "")
        except Exception as exc:
            self.emit({"type": "error", "message": f"Call speech recognition failed: {exc}"})
            return
        text = tr.text.strip()
        # Phone lines hiss: Whisper turns that into "Thank you." / "you" / one word looped. Never answer those.
        if not text or re.fullmatch(r"\W*(you|thank you|thanks for watching)\W*", text, re.I) \
                or is_hallucination(text) or (tr.confidence < 0.25 and len(text.split()) <= 2):
            return
        await self.caller_said(rt.call.id, text, t_end)

    async def caller_said(self, call_id: str, text: str, t_end: float | None = None) -> None:
        rt = self.calls.get(call_id)
        if not rt:
            return
        rt.call.transcript.append({"role": "caller", "text": text, "t": time.time()})
        self.emit({"type": "call_transcript", "call_id": call_id, "role": "caller", "text": text})
        if not rt.call.ai_active:
            return
        if rt.turn and not rt.turn.done():
            rt.turn.cancel()
        rt.turn = asyncio.create_task(self._reply(rt, text, t_end or time.perf_counter()))

    async def _reply(self, rt: _CallRuntime, text: str, t_end: float) -> None:
        first = True
        rt.reply_spoken = []
        try:
            async for sentence in rt.conversation.reply(text):
                await self._speak(rt, sentence, reply=True)
                if first:
                    rt.latencies.append(time.perf_counter() - t_end)
                    first = False
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # Ollama down / timed out: the caller must not be left in silence
            self.emit({"type": "error", "message": f"Call reply failed: {exc}"})
            if rt.call.id in self.calls:
                await self._speak(rt, "Sorry, I'm having trouble right now. Could you say that again?")

    async def _speak(self, rt: _CallRuntime, text: str, role: str = "jarvis", reply: bool = False) -> None:
        clip = await asyncio.to_thread(self.tts.synthesize, text)
        payload = to_phone(clip, self.tts.sample_rate, rt.call.codec)
        start = max(time.monotonic(), rt.speaking_until)
        if reply:
            rt.reply_spoken.append((text, start))
        rt.speaking_until = start + len(payload) / 8000
        rt.call.transcript.append({"role": role, "text": text, "t": time.time()})
        self.emit({"type": "call_transcript", "call_id": rt.call.id, "role": role, "text": text})
        await self.providers[rt.call.provider].send_audio(rt.call, payload)

    # ---------------- simulator ----------------
    def simulate_incoming(self, caller: str = "Test caller") -> dict:
        mock: MockProvider = self.providers["mock"]  # type: ignore[assignment]
        return mock.simulate_incoming(caller).to_dict()

    def simulate_audio(self, call_id: str, pcm16k: np.ndarray) -> None:
        """Push 16 kHz speech through the real phone path: G.711 encode → decode → VAD → STT."""
        rt = self.calls.get(call_id)
        if not rt:
            return
        payload = to_phone(pcm16k, 16000, rt.call.codec) + to_phone(np.zeros(16000, np.int16), 16000, rt.call.codec)
        self.providers[rt.call.provider].on_audio(rt.call, payload)
