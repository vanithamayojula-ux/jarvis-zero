"""FastAPI backend: REST control + WebSocket event stream. Binds to 127.0.0.1 only."""
from __future__ import annotations

import asyncio
import re
import sys
import json
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import numpy as np
from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .audio import list_devices
from .browser import EdgeController
from .hotkey import GlobalHotkey
from .components import OllamaLLM, SileroVAD, SmartTurn, WakeWord, WhisperSTT, available_voices, make_tts
from .cloud import CloudLLM, CloudSTT, CloudTTS, groq_key
from .memory import Memory
from .config import DATA_DIR, ROOT, Settings, api_token
from .conversation import system_prompt
from .engine import Engine
from .telephony.manager import TelephonyManager
from .tools import Reminders, ToolBox

PORT = 8600
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "testserver"}
ALLOWED_ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}", "http://localhost:5173", "http://127.0.0.1:5173"}
FRONTEND = ROOT / "frontend" / "dist"


class AppState:
    settings: Settings
    engine: Engine
    tel: TelephonyManager
    reminders: Reminders
    llm: OllamaLLM


S = AppState()


def _site_guard(settings: Settings):
    """Blocklists for JARVIS's Edge (data/blocklist). Downloaded in the background when missing or a week old."""
    from .safety import SiteGuard
    guard = SiteGuard(DATA_DIR / "blocklist", settings.get("site_allowlist"))
    if settings.get("blocklist_auto_update") and guard.needs_refresh() and not getattr(S, "no_hotkey", False):
        threading.Thread(target=guard.refresh, daemon=True, name="blocklist-refresh").start()
    return guard


def build(settings: Settings, mic_factory=None, speaker_factory=None, llm=None, stt=None, tts=None, vad_factory=None):
    """Create every component. Arguments allow tests to inject fakes."""
    S.settings = settings
    S.llm = llm or OllamaLLM(settings.get("ollama_url"), settings.get("llm_model"), settings.get("llm_keep_alive"))
    stt = stt or WhisperSTT(settings.get("stt_model"))
    if llm is None and mic_factory is None and settings.get("cloud_ai") and groq_key():
        # Groq's free tier in front, the local models behind it (used whenever the cloud can't answer)
        S.llm = CloudLLM(settings.get("ollama_url"), settings.get("llm_model"), settings.get("llm_keep_alive"),
                         groq_key(), settings.get("cloud_llm_model"))
        stt = CloudSTT(stt, groq_key(), settings.get("cloud_stt_model"))
    tts = tts or make_tts(settings.get("tts_voice"), settings.get("speaking_rate"))
    vad_factory = vad_factory or SileroVAD
    S.reminders = Reminders(DATA_DIR / "reminders.json")
    edge = EdgeController(DATA_DIR / settings.get("edge_profile"), settings.get("edge_debug_port"),
                          guard=_site_guard(settings)) if settings.get("browser_control") else None
    desktop = None
    if mic_factory is None and sys.platform == "win32":   # the real thing only: tests must never touch your desktop
        try:
            from .desktop import Desktop
            desktop = Desktop()
        except Exception:
            desktop = None                                 # uiautomation missing: Edge-only control, as before
    system = None
    if mic_factory is None and sys.platform == "win32":    # tests never touch the real volume or screen
        from .system import SystemControls
        system = SystemControls()
    tools = ToolBox(settings, S.reminders, edge=edge, desktop=desktop, system=system)
    kwargs = {k: v for k, v in {"mic_factory": mic_factory, "speaker_factory": speaker_factory}.items() if v}
    if isinstance(S.llm, CloudLLM) and settings.get("cloud_tts"):
        # Orpheus for chat replies; the local voice for command confirmations and when the free limit runs out
        tts = CloudTTS(tts, groq_key(), settings.get("cloud_tts_voice"),
                       kind=lambda: getattr(getattr(S.engine, "conversation", None), "reply_kind", "chat"))
    S.memory = Memory(DATA_DIR / "memory.json") if settings.get("memory") is not False else None
    S.engine = Engine(settings, vad_factory(), stt, S.llm, tts, tools, memory=S.memory, **kwargs)
    if mic_factory is None and settings.get("wake_engine"):
        try:
            S.engine.wake_model = WakeWord()               # "hey jarvis", used in hands-free + wake word mode
        except Exception as exc:                            # not installed: the transcript check, as before
            S.engine.emit({"type": "error", "message": f"Wake word engine unavailable: {exc}"})
    if mic_factory is None and settings.get("smart_turn") and SmartTurn.PATH.exists():
        try:
            S.engine.turn_model = SmartTurn()
        except Exception as exc:                            # broken file: plain pause detection, as before
            S.engine.emit({"type": "error", "message": f"Smart Turn unavailable: {exc}"})
    tools.summarize = S.engine.conversation.summarize
    S.tel = TelephonyManager(settings, S.llm, stt, tts, vad_factory, S.engine.emit, tools=tools)
    S.reminders.on_due = lambda r: (S.engine.emit({"type": "reminder", "reminder": r}),
                                    S.engine.say_now(f"Reminder: {r['text']}."))


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not getattr(S, "engine", None):
        build(Settings())
    await S.engine.start()
    await S.tel.start()
    S.hotkey = None
    combos = [c for c in [S.settings.get("listen_hotkey"), "ctrl+alt+space", "ctrl+alt+j", "ctrl+shift+space"] if c]
    if combos and sys.platform == "win32" and not getattr(S, "no_hotkey", False):
        loop = asyncio.get_running_loop()
        S.hotkey = GlobalHotkey(list(dict.fromkeys(combos)), lambda: loop.call_soon_threadsafe(S.engine.listen_once)).start()
        S.engine.emit({"type": "hotkey", "combo": S.hotkey.active, "error": S.hotkey.error})
    if isinstance(S.llm, OllamaLLM):
        asyncio.create_task(_warm())
    yield
    if getattr(S, "hotkey", None):
        S.hotkey.stop()
    await S.tel.stop()
    await S.engine.shutdown()
    S.reminders.stop()
    if isinstance(S.llm, OllamaLLM):
        await S.llm.unload()


async def _warm():
    try:
        await S.llm.warm(system_prompt(S.settings, "local"))
        S.engine.emit({"type": "llm", "ready": True})
    except Exception as exc:
        S.engine.emit({"type": "error", "message": f"Ollama not reachable: {exc}"})


app = FastAPI(title="JARVIS Zero", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def local_only(request: Request, call_next):
    # Blocks DNS-rebinding and cross-site requests: only this machine's own pages may talk to JARVIS.
    host = request.headers.get("host", "")
    origin = request.headers.get("origin")
    if host not in ALLOWED_HOSTS or (origin and origin not in ALLOWED_ORIGINS):
        return _forbidden()
    return await call_next(request)


def _forbidden():
    from fastapi.responses import JSONResponse
    return JSONResponse({"detail": "forbidden"}, status_code=403)


def auth(request: Request) -> None:
    token = api_token()
    if token and request.headers.get("x-jarvis-token") != token:
        raise HTTPException(401, "bad token")


# ---------------- models ----------------
class TextIn(BaseModel):
    text: str


class SettingsIn(BaseModel):
    changes: dict


class CallIn(BaseModel):
    provider: str = "mock"
    destination: str
    consent: bool = False


class SimSay(BaseModel):
    call_id: str
    text: str


class CallId(BaseModel):
    call_id: str


# ---------------- core ----------------
@app.get("/api/status", dependencies=[Depends(auth)])
async def status():
    return {"engine": S.engine.status(), "telephony": S.tel.status(),
            "listen_hotkey": getattr(getattr(S, "hotkey", None), "active", None), "llm_ready": await S.llm.health()
            if isinstance(S.llm, OllamaLLM) else True,
            "cloud": S.llm.status() if isinstance(S.llm, CloudLLM) else {"cloud": False},
            "voice": {"cloud": isinstance(S.engine.tts, CloudTTS), "last": getattr(S.engine.tts, "last_source", "local"),
                      "cloud_voice": getattr(S.engine.tts, "voice", None)},
            "smart_turn": S.engine.turn_model is not None,
            "models": {"stt": S.settings.get("stt_model"),
            "llm": S.settings.get("llm_model"), "tts": S.settings.get("tts_voice")}}


@app.get("/api/settings", dependencies=[Depends(auth)])
async def get_settings():
    return S.settings.all()


_RANGES = {"smart_turn_max_ms": (500, 6000), "followup_s": (0, 600), "followup_min_confidence": (0, 1), "echo_coupling": (0, 5), "echo_margin": (1, 20),
           "noise_floor": (0, 0.5), "vad_threshold": (0.05, 0.99), "silence_ms": (150, 5000), "hold_ms": (0, 5000),
           "max_utterance_s": (2, 120), "history_turns": (1, 50), "speaking_rate": (0.5, 2.0),
           "edge_debug_port": (1024, 65535)}
_CHOICES = {"mode": {"push_to_talk", "hands_free"}, "activation": {"wake_word", "voice"},
            "cloud_tts_voice": {"troy", "austin", "daniel", "autumn", "diana", "hannah"},
            "browser": {"edge", "chrome", "default"}}
_STT_MODELS = {"tiny.en", "base.en", "small.en", "medium.en", "tiny", "base", "small", "medium",
               "large-v3", "distil-small.en", "distil-medium.en", "distil-large-v3"}
_TEL_RANGES = {"sip_port": (1024, 65535), "rtp_port_min": (1024, 65535), "rtp_port_max": (1024, 65535)}


def _validate_settings(ch: dict) -> None:
    """Reject values that would break the voice loop (e.g. silence_ms="abc" kills turn detection) or weaken a gate."""
    from .config import DEFAULTS, PERSONALITIES

    def bad(msg: str):
        raise HTTPException(400, msg)

    def number(key, v, lo, hi):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
            bad(f"{key} must be a number from {lo} to {hi}")

    unknown = set(ch) - set(DEFAULTS)
    if unknown:
        bad(f"unknown settings: {sorted(unknown)}")
    for key, v in ch.items():
        default = DEFAULTS[key]
        if key in _RANGES:
            number(key, v, *_RANGES[key])
        elif key in _CHOICES and v not in _CHOICES[key]:
            bad(f"{key} must be one of {sorted(_CHOICES[key])}")
        elif key == "personality" and v not in PERSONALITIES:
            bad(f"personality must be one of {sorted(PERSONALITIES)}")
        elif key == "wake_word" and not (isinstance(v, str) and re.fullmatch(r"[a-zA-Z]{3,20}", v.strip())):
            bad("wake_word must be one word of 3-20 letters")
        elif key == "stt_model" and v not in _STT_MODELS:
            bad(f"stt_model must be one of {sorted(_STT_MODELS)}")
        elif key == "tts_voice" and v not in available_voices():
            bad(f"unknown voice {v!r}")
        elif key in ("input_device", "output_device"):
            if v is not None:
                devs = list_devices()["inputs" if key == "input_device" else "outputs"]
                if isinstance(v, bool) or not isinstance(v, int) or v not in {d["index"] for d in devs}:
                    bad(f"{key}: no such audio device {v!r}")
        elif key == "listen_hotkey" and v:
            from .hotkey import parse
            try:
                parse(str(v))
            except ValueError as exc:
                bad(str(exc))
        elif key == "ollama_url" and not (isinstance(v, str) and re.match(r"^https?://[\w.\-]+(:\d+)?/?$", v)):
            bad("ollama_url must look like http://127.0.0.1:11434")
        elif key == "user_name" and not (isinstance(v, str) and len(v) <= 60):
            bad("user_name must be text (up to 60 characters)")
        elif key == "name_hints" and not (isinstance(v, list) and len(v) <= 60 and
                                          all(isinstance(x, str) and 0 < len(x) <= 40 for x in v)):
            bad("name_hints must be a list of up to 60 names (each up to 40 characters)")
        elif key == "telephony":
            if not isinstance(v, dict):
                bad("telephony must be an object")
            for tk, tv in v.items():
                if tk not in default:
                    bad(f"unknown telephony setting {tk!r}")
                if tk in _TEL_RANGES:
                    number(f"telephony.{tk}", tv, *_TEL_RANGES[tk])
                elif tk == "real_calls_enabled" and tv is not False:
                    bad("Real phone (PSTN) calls cannot be enabled: there is no zero-cost route.")
                elif tk == "provider" and tv not in ("mock", "sip_lan"):
                    bad("telephony.provider must be mock or sip_lan")
                elif tk in ("sip_allowed_ips", "owner_ips") and not (isinstance(tv, list) and all(isinstance(x, str) for x in tv)):
                    bad(f"telephony.{tk} must be a list of IP addresses / subnets")
                elif isinstance(default[tk], bool) and not isinstance(tv, bool):
                    bad(f"telephony.{tk} must be true or false")
                elif isinstance(default[tk], str) and not isinstance(tv, str):
                    bad(f"telephony.{tk} must be text")
        elif isinstance(default, bool) and not isinstance(v, bool):
            bad(f"{key} must be true or false")
        elif isinstance(default, str) and not isinstance(v, str):
            bad(f"{key} must be text")
        elif isinstance(default, (dict, list)) and not isinstance(v, type(default)):
            bad(f"{key} must be a {type(default).__name__}")
        elif isinstance(default, (int, float)) and not isinstance(default, bool) and key not in _RANGES and \
                (isinstance(v, bool) or not isinstance(v, (int, float))):
            bad(f"{key} must be a number")


@app.patch("/api/settings", dependencies=[Depends(auth)])
async def patch_settings(body: SettingsIn):
    await asyncio.to_thread(_validate_settings, body.changes)
    try:
        new = S.settings.update(body.changes)
    except KeyError as exc:
        raise HTTPException(400, str(exc))
    ch = body.changes
    if "speaking_rate" in ch and hasattr(S.engine.tts, "set_rate"):
        S.engine.tts.set_rate(ch["speaking_rate"])
    if "cloud_tts_voice" in ch and isinstance(S.engine.tts, CloudTTS):
        S.engine.tts.voice = ch["cloud_tts_voice"]
    if "tts_voice" in ch:
        tts = await asyncio.to_thread(make_tts, ch["tts_voice"], new["speaking_rate"])
        if isinstance(S.engine.tts, CloudTTS):              # keep Orpheus in front of the new local voice
            old = S.engine.tts
            tts = CloudTTS(tts, old.key, old.voice, old.model, kind=old.kind)
        S.engine.tts = S.tel.tts = tts
        S.engine.speaker.close()
        S.engine.speaker = S.engine.speaker_factory(tts.sample_rate, new["output_device"])
    if "stt_model" in ch:
        stt = await asyncio.to_thread(WhisperSTT, ch["stt_model"])
        if isinstance(S.engine.stt, CloudSTT):             # keep the cloud in front of the new local model
            stt = CloudSTT(stt, S.engine.stt.key, S.engine.stt.model_name)
        S.engine.stt = S.tel.stt = stt
    if "llm_model" in ch and isinstance(S.llm, OllamaLLM):
        S.llm.model = ch["llm_model"]
        asyncio.create_task(_warm())
    if "output_device" in ch:
        S.engine.speaker.close()
        S.engine.speaker = S.engine.speaker_factory(S.engine.tts.sample_rate, ch["output_device"])
    if "input_device" in ch and S.engine.listening:
        S.engine.stop_listening()
        S.engine.start_listening()
    tel = ch.get("telephony", {})
    if "sip_lan_enabled" in tel:
        await (S.tel.enable_sip_lan() if tel["sip_lan_enabled"] else S.tel.disable_sip_lan())
    S.engine.emit({"type": "settings", "settings": new})
    return new


@app.get("/api/devices", dependencies=[Depends(auth)])
async def devices():
    return list_devices()


@app.get("/api/voices", dependencies=[Depends(auth)])
async def voices():
    return {"voices": available_voices()}


@app.get("/api/llm-models", dependencies=[Depends(auth)])
async def llm_models():
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(S.settings.get("ollama_url") + "/api/tags")
        return {"models": [m["name"] for m in r.json().get("models", [])]}
    except Exception:
        return {"models": [], "error": "Ollama is not running"}


@app.post("/api/listen/start", dependencies=[Depends(auth)])
async def listen_start():
    S.engine.start_listening()
    return S.engine.status()


@app.post("/api/listen/stop", dependencies=[Depends(auth)])
async def listen_stop():
    S.engine.stop_listening()
    return S.engine.status()


@app.post("/api/listen/once", dependencies=[Depends(auth)])
async def listen_once():
    """Same as the global talk hotkey (the floating bot's Talk button uses it)."""
    S.engine.listen_once()
    return S.engine.status()


@app.post("/api/ptt/{action}", dependencies=[Depends(auth)])
async def ptt(action: str):
    if action not in ("down", "up"):
        raise HTTPException(404)
    S.engine.set_ptt(action == "down")
    return S.engine.status()


@app.post("/api/interrupt", dependencies=[Depends(auth)])
async def interrupt():
    S.engine.interrupt()
    return {"ok": True}


@app.post("/api/emergency-stop", dependencies=[Depends(auth)])
async def emergency_stop():
    S.engine.emergency_stop()
    await S.tel.emergency_stop()
    return {"ok": True, "telephony_locked": S.tel.gate.locked}


@app.post("/api/emergency-reset", dependencies=[Depends(auth)])
async def emergency_reset():
    S.tel.gate.unlock()
    return {"ok": True}


@app.post("/api/text", dependencies=[Depends(auth)])
async def text(body: TextIn):
    t = body.text.strip()
    if not t:
        raise HTTPException(400, "empty message")
    if len(t) > 4000:
        raise HTTPException(400, "message too long (max 4000 characters)")
    S.engine.submit_text(t)
    return {"ok": True}


@app.post("/api/confirm/{answer}", dependencies=[Depends(auth)])
async def confirm(answer: str):
    if answer not in ("yes", "no"):
        raise HTTPException(404)
    S.engine.submit_text("yes" if answer == "yes" else "no")
    return {"ok": True}


@app.post("/api/conversation/new", dependencies=[Depends(auth)])
async def new_conversation():
    S.engine.new_conversation()
    S.engine.tools.summarize = S.engine.conversation.summarize
    return {"id": S.engine.conversation.id}


@app.get("/api/conversation/summary", dependencies=[Depends(auth)])
async def summary():
    return {"summary": await S.engine.conversation.summarize()}


@app.get("/api/conversations", dependencies=[Depends(auth)])
async def conversations():
    folder = DATA_DIR / "conversations"
    items = []
    for p in sorted(folder.glob("*.jsonl"), reverse=True)[:50]:
        msgs = _read_jsonl(p)
        first = next((m.get("text", "") for m in msgs if m.get("role") == "user"), "")
        items.append({"id": p.stem, "turns": len(msgs), "preview": str(first)[:80],
                      "channel": msgs[0].get("channel", "") if msgs else ""})
    return {"conversations": items}


def _read_jsonl(p: Path) -> list[dict]:
    """A log cut off mid-line (power loss, crash) must not break the whole history page."""
    out = []
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        if isinstance(m, dict):
            out.append(m)
    return out


@app.get("/api/conversations/{cid}", dependencies=[Depends(auth)])
async def conversation(cid: str):
    p = DATA_DIR / "conversations" / f"{Path(cid).name}.jsonl"
    if not p.exists():
        raise HTTPException(404)
    return {"id": cid, "messages": _read_jsonl(p)}


@app.post("/api/test/local", dependencies=[Depends(auth)])
async def local_test():
    """Self-test of each local component, with real timings."""
    out: dict = {}
    t = time.perf_counter()
    clip = await asyncio.to_thread(S.engine.tts.synthesize, "Systems check complete. All local components are online.")
    out["tts_s"] = round(time.perf_counter() - t, 3)
    t = time.perf_counter()
    pcm = clip if S.engine.tts.sample_rate == 16000 else np.interp(
        np.arange(int(len(clip) * 16000 / S.engine.tts.sample_rate)) * (S.engine.tts.sample_rate / 16000),
        np.arange(len(clip)), clip).astype(np.int16)
    tr = await asyncio.to_thread(S.engine.stt.transcribe, pcm, "")
    out["stt_s"], out["stt_heard"] = round(time.perf_counter() - t, 3), tr.text
    t = time.perf_counter()
    try:
        reply = await S.llm.complete([{"role": "user", "content": "Reply with exactly: ready"}])
        out["llm_s"], out["llm_reply"] = round(time.perf_counter() - t, 3), reply.strip()[:60]
    except Exception as exc:
        out["llm_error"] = str(exc)
    S.engine.speaker.play(clip)
    out["played"] = True
    return out


# ---------------- telephony ----------------
@app.get("/api/telephony/status", dependencies=[Depends(auth)])
async def tel_status():
    return S.tel.status()


@app.get("/api/telephony/phone-setup", dependencies=[Depends(auth)])
async def tel_phone_setup():
    """SIP addresses + offline QR codes + allowlist for the "Call JARVIS from your phone" card."""
    out = await asyncio.to_thread(S.tel.phone_setup)
    out["firewall_rules"] = await asyncio.to_thread(_phone_firewall_rules)
    return out


def _phone_firewall_rules() -> bool | None:
    """True if scripts/setup-phone.ps1 has added its firewall rules (None = could not check)."""
    import subprocess
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", "name=JARVIS Zero phone (UDP)"],
                           capture_output=True, text=True, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0 and "JARVIS Zero phone" in r.stdout
    except Exception:
        return None


def _call_or_404(call_id: str) -> None:
    if call_id not in S.tel.calls:
        raise HTTPException(404, "no such active call (it may have ended)")


@app.post("/api/telephony/call", dependencies=[Depends(auth)])
async def tel_call(body: CallIn):
    decision, call = await S.tel.place_call(body.provider, body.destination, body.consent)
    return {"allowed": decision.allowed, "reason": decision.reason, "call": call}


@app.post("/api/telephony/simulate/incoming", dependencies=[Depends(auth)])
async def sim_incoming():
    return S.tel.simulate_incoming("Simulated caller")


@app.post("/api/telephony/simulate/say", dependencies=[Depends(auth)])
async def sim_say(body: SimSay):
    _call_or_404(body.call_id)
    await S.tel.caller_said(body.call_id, body.text)
    return {"ok": True}


@app.post("/api/telephony/hangup", dependencies=[Depends(auth)])
async def tel_hangup(body: CallId):
    _call_or_404(body.call_id)
    await S.tel.hangup(body.call_id)
    return {"ok": True}


@app.post("/api/telephony/handoff", dependencies=[Depends(auth)])
async def tel_handoff(body: CallId):
    _call_or_404(body.call_id)
    await S.tel.handoff(body.call_id)
    return {"ok": True}


@app.post("/api/telephony/resume-ai", dependencies=[Depends(auth)])
async def tel_resume(body: CallId):
    _call_or_404(body.call_id)
    await S.tel.resume_ai(body.call_id)
    return {"ok": True}


@app.post("/api/telephony/operator-say", dependencies=[Depends(auth)])
async def tel_operator(body: SimSay):
    _call_or_404(body.call_id)
    await S.tel.operator_say(body.call_id, body.text)
    return {"ok": True}


# ---------------- live events ----------------
@app.websocket("/ws")
async def ws(websocket: WebSocket):
    origin = websocket.headers.get("origin")
    host = websocket.headers.get("host", "")
    token = api_token()
    if host not in ALLOWED_HOSTS or (origin and origin not in ALLOWED_ORIGINS) or \
            (token and websocket.query_params.get("token") != token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    q = S.engine.subscribe()
    await websocket.send_json({"type": "hello", "status": S.engine.status(), "telephony": S.tel.status()})

    async def ticker():
        while True:  # mic level + status heartbeat for the indicators
            await asyncio.sleep(0.25)
            await websocket.send_json({"type": "level", "mic": S.engine.status()["mic_level"], "state": S.engine.state})

    tick = asyncio.create_task(ticker())
    try:
        while True:
            await websocket.send_json(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        tick.cancel()
        S.engine.unsubscribe(q)


if FRONTEND.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        return FileResponse(FRONTEND / "index.html")
