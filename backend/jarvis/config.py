"""Settings: defaults < data/settings.json < environment (.env). Secrets only ever come from the environment."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("JARVIS_DATA_DIR", ROOT / "data"))
MODELS_DIR = ROOT / "models"
SETTINGS_PATH = DATA_DIR / "settings.json"

load_dotenv(ROOT / ".env")

def _known_folder(csidl: int, fallback: str) -> str:
    """Ask Windows where Desktop/Documents really are (OneDrive often relocates them)."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
            return buf.value
    except Exception:
        pass
    return str(Path.home() / fallback)


PERSONALITIES = {
    "human": ("You are JARVIS, an AI companion who talks like a sharp, warm friend: relaxed, curious, a bit witty, "
              "and honest. You react to what the person actually said, share a quick opinion when asked, and think "
              "before you answer."),
    "jarvis": "You are JARVIS, a calm, dry-witted British AI assistant. Polite, efficient, lightly humorous.",
    "friendly": "You are JARVIS, a warm, upbeat AI assistant who sounds like a helpful friend.",
    "concise": "You are JARVIS, a no-nonsense AI assistant. Answer in as few words as possible.",
}

DEFAULTS: dict = {
    "mode": "push_to_talk",              # push_to_talk | hands_free
    "listen_hotkey": "ctrl+alt+j",       # works in any window: press, speak, JARVIS answers (no wake word)
    "activation": "wake_word",           # hands-free trigger: wake_word | voice
    "wake_word": "jarvis",
    "followup_s": 15,                    # after a reply, keep talking without the wake word for this long
    "followup_min_confidence": 0.5,      # follow-ups without the wake word must be this clear (filters video audio)
    "voice_log": True,                   # write what the mic heard to data/voice.log (local, for troubleshooting)
    "input_device": None,                # sounddevice index or None for system default
    "output_device": None,
    "barge_in": True,                    # stop speaking when the user starts talking
    "echo_coupling": 0.3,                # fraction of speaker output the mic hears (run scripts/calibrate_echo.py)
    "echo_margin": 3.0,                  # user voice must beat the expected echo by this factor to barge in
    "noise_floor": 0.002,
    "vad_threshold": 0.5,
    "silence_ms": 550,                   # end-of-turn silence
    "hold_ms": 900,                      # extra wait when the sentence sounds unfinished
    "max_utterance_s": 30,
    "stt_model": "base.en",
    "llm_model": "llama3.2:3b",
    "ollama_url": "http://127.0.0.1:11434",
    "llm_keep_alive": -1,                # keep the model loaded while JARVIS runs (unloaded on exit)
    "history_turns": 8,
    "llm_tool_calling": False,           # let the LLM pick tools too (slow + unreliable on 3B CPU models)
    "personality": "human",
    "user_name": "",
    "tts_voice": "en_GB-northern_english_male-medium",  # most natural fast voice; "kokoro:am_michael" = more human, slower
    "speaking_rate": 1.0,                # 1.0 normal, >1 faster
    "approved_apps": {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "paint": "mspaint.exe",
        "file explorer": "explorer.exe",
        "edge": "msedge",
        "chrome": "chrome",
        "vs code": "code",
        "cursor": str(Path.home() / "AppData" / "Local" / "Programs" / "cursor" / "Cursor.exe"),
    },
    # Shortcuts on these desktops count as approved apps ("open valorant"), except the blocked ones.
    "shortcut_dirs": [_known_folder(0x10, "Desktop"), _known_folder(0x19, "Desktop")],
    "blocked_shortcuts": ["ShutDown", "MinGW Installer", "JARVIS", "JARVIS Zero"],
    "approved_sites": ["google.com", "youtube.com", "github.com", "wikipedia.org", "gmail.com", "maps.google.com"],
    "browser": "edge",                   # "edge" = JARVIS's own controllable Edge window; or "chrome", "default"
    "browser_control": True,             # let JARVIS type, click and read pages in its own Edge window
    "edge_debug_port": 9333,             # local-only DevTools port for that window
    "edge_profile": "edge-agent",        # data/<this>: JARVIS's own clean Edge profile (no accounts, sign-in blocked)
    "edge_open_any_site": True,          # in that clean Edge, open any website without asking first (blocklists apply)
    "site_allowlist": [],                # sites you've decided are safe even if a blocklist disagrees
    "blocklist_auto_update": True,       # refresh the free malware/phishing/scam lists once a week
    "desktop_control": True,             # click / type / press keys in any app (never terminals, password boxes…)
    "cloud_ai": True,                    # use Groq's free tier when GROQ_API_KEY is in .env (local models as backup)
    "cloud_llm_model": "qwen/qwen3.8-27b",
    "cloud_stt_model": "whisper-large-v3-turbo",
    "memory": True,                      # remember facts about you + the last thread, in data/memory.json (local)
    "memory_auto": True,                 # pick facts up from chat by itself (else only "remember that…")
    # Names the speech recogniser should expect (people, artists, songs). Add your own in the dashboard settings.
    "name_hints": ["Hanumankind", "Run It Up", "Big Dawgs", "Arijit Singh", "Rahul", "Spotify", "Valorant"],
    "smart_turn": True,                  # Pipecat Smart Turn v3: a mid-sentence pause doesn't end your turn
    "wake_engine": True,                 # openWakeWord "hey jarvis" (hands-free + wake word mode)
    "smart_turn_max_ms": 2500,           # longest it waits through a pause that sounds unfinished
    "cloud_tts": True,                   # Groq's Orpheus voice for chat replies (~100 a day free; local voice after)
    "cloud_tts_voice": "troy",           # troy, austin, daniel (male) · autumn, diana, hannah (female)
    "cloud_interpret": True,             # messy commands the rules miss are restated by the cloud model, then routed
    "search_dirs": [_known_folder(0x10, "Desktop"), _known_folder(0x05, "Documents"), str(Path.home() / "Downloads")],
    "allowed_commands": {
        "ip address": {"argv": ["ipconfig"], "confirm": False},
        "disk space": {"argv": ["powershell", "-NoProfile", "-Command",
                                "Get-PSDrive -PSProvider FileSystem | Select-Object Name,Used,Free | Format-Table -AutoSize"],
                       "confirm": False},
        "flush dns": {"argv": ["ipconfig", "/flushdns"], "confirm": True},
        "lock screen": {"argv": ["rundll32.exe", "user32.dll,LockWorkStation"], "confirm": True},
    },
    "save_conversations": True,
    "telephony": {
        "provider": "mock",              # mock | sip_lan
        "real_calls_enabled": False,     # PSTN calling: off. No zero-cost PSTN route exists.
        "sip_lan_enabled": False,        # local SIP-to-SIP over your own network (no phone numbers, no cost)
        "sip_bind_host": "0.0.0.0",
        "sip_port": 5062,
        # Who may call in: single IPs or CIDR subnets. Loopback is always allowed. Empty = the safe default below.
        # Default = Windows Mobile Hotspot clients only (phone joined to the laptop's hotspot), never the whole Wi-Fi.
        "sip_allowed_ips": ["192.168.137.0/24"],
        "rtp_port_min": 40000,            # RTP media ports (firewall rule in scripts/setup-phone.ps1 matches this)
        "rtp_port_max": 40100,
        # Owner mode: calls from these IPs/subnets get the full local toolset (open apps etc., same confirmations).
        # Empty = no tools on calls. Use only for your own phone's address on a network only you are on.
        "owner_ips": [],
        "record_calls": False,
        "announce_ai": True,
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Settings:
    """Thread-safe settings store persisted to data/settings.json (never contains secrets)."""

    def __init__(self, path: Path = SETTINGS_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._data = copy.deepcopy(DEFAULTS)
        if path.exists():
            try:
                self._data = _merge(DEFAULTS, json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass

    def get(self, key: str, default=None):
        with self._lock:
            return copy.deepcopy(self._data.get(key, default))

    def all(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def update(self, changes: dict) -> dict:
        with self._lock:
            unknown = set(changes) - set(DEFAULTS)
            if unknown:
                raise KeyError(f"unknown settings: {sorted(unknown)}")
            self._data = _merge(self._data, changes)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
            return copy.deepcopy(self._data)


def api_token() -> str:
    return os.environ.get("JARVIS_API_TOKEN", "")
