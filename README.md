# Spidey

(The project was called JARVIS Zero; code, folders and the `jarvis` package keep that name.)

A personal voice assistant for Windows that runs **entirely on your laptop, for ₹0**. No API keys, no subscriptions, no cloud. After installation, conversations work offline.

- Speech in: **Silero VAD** + **faster-whisper** (`base.en`, CPU int8)
- Brain: **Ollama** with `llama3.2:3b` (CPU)
- Speech out: **Piper** (default `en_GB-northern_english_male-medium`), optional **Kokoro-82M** for the most human-sounding voice (see [Voices](#voices))
- Backend: **FastAPI** on `127.0.0.1:8600` · Dashboard: **React + TypeScript** (Vite)

## Install and run

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1   # once (about 4 GB of downloads incl. the LLM)
.\start-jarvis.bat                                       # opens http://127.0.0.1:8600
python scripts\calibrate_echo.py                          # optional, makes interrupting Spidey reliable
```

Needs Python 3.10–3.12, Node.js 18+, and [Ollama](https://ollama.com/download) (free).

## Using it

- **Spidey shortcut:** tap **Right Ctrl** on its own to open or close spidey's chat bar (holding it, or Right Ctrl + another key, does nothing; turn off in spidey's right-click menu). Ctrl + Alt + 0 still works.
- **Spidey panel:** double-click spidey (or its ring's home button, or right-click → Open Spidey panel) for a small window beside it with the conversation, a message box, the Chat/Voice switch, status and your next reminder. Starting Spidey opens this panel instead of a browser tab; the full dashboard is a link at the bottom of the panel.
- **Setup (Chat or Voice):** double-click **setup.bat**. *Chat mode* = you type (click spidey or **Ctrl + Alt + 0**), Spidey answers in text, silently. No mic, no speaker and no voice models in memory (about 114 MB RAM vs 211 MB), and no dashboard tab on start. *Voice mode* = talk and hear. Switch any time: right-click spidey → **Mode**, or say/type "chat mode" / "voice mode". In chat mode the talk shortcut opens spidey's bar.
- **Type exactly:** "type What is RAM, and why?" types everything after *type* word for word, capitals and punctuation kept, into the box Spidey can see. Add "in the search bar" to use the page's search box, and "and press enter" / "and search" to submit.
- **Internet lookups (optional):** setup.bat takes a free search key (Tavily recommended; Brave or Serper also work) and stores it in `.env`. Then "look up the iPhone 17 price", "what's the weather in Hyderabad?" or "who won the match yesterday?" are answered from fresh web results. Only the search words are sent; Spidey's own model writes the answer from the results, which it treats as data, never as instructions. Without a key it offers a Google search as before.
- **Talk from any window:** press **Ctrl + Alt + J**, hear the beep, speak — Spidey answers when you stop (no wake word, nothing to hold). Pressing it while Spidey talks interrupts it. Change with `listen_hotkey`.
- **Start it:** press **Ctrl + Alt + Shift + J** anywhere (or double-click **JARVIS Zero** on the Desktop). It starts in the background, opens the dashboard and starts listening. Pressing it again just reopens the dashboard. Stop with `stop-jarvis.ps1`.
- **Web apps in a browser:** "open WhatsApp in Chrome", "open Gmail in Edge", "search for cricket scores", "open Chrome and then open YouTube". Known web apps (WhatsApp, Gmail, YouTube, Instagram, ChatGPT, Drive…) open without confirmation; websites use `browser` from settings (Chrome).

- **Push-to-talk** (default): hold the button or **Space**, speak, release. Pressing interrupts Spidey.
- **Hands-free**: say **"Jarvis, …"**. For about 8 s after a reply (and while Spidey is speaking) you can follow up without the wake word. Or set activation to *Any speech*.
- **Interrupt**: start talking over Spidey (hands-free), press **Esc**, or click *Interrupt*. Spidey only remembers the part you actually heard.
- **Emergency stop** (top right): stops speech, turns the mic off, and locks all outgoing calls until you unlock them.
- **Spidey's own Edge window** (separate profile in `data/edge-profile`, controlled locally through Edge's DevTools port on 127.0.0.1:9333; link WhatsApp/YouTube there once). Say: "search YouTube for lofi", "play anime fights on YouTube", "play the second video", "pause / mute / skip the ad / next video", "click on subscribe", "click the search box", "type hello", "type weather in Mumbai and press enter", "scroll down", "go back", "close tab", "what's on this page", "summarize this page". **Sending or posting always asks first** (Enter on WhatsApp/Gmail/X…, clicking Send/Post/Comment). Turn off with `browser_control: false`.
- **Honesty guard:** if the small AI claims it did something in chat ("I've opened…", "I'll type…"), that sentence is replaced with an honest "I can't do that by just talking about it" and a hint.
- **Hands-free and speakers:** after Spidey opens YouTube/Netflix/Spotify, follow-ups need "Jarvis" again for 30 minutes, so the video's own speech isn't treated as you. Whisper's "okay, okay, okay…" loops are dropped. Headphones still work best.
- **Your apps:** every shortcut on your Desktop counts as an approved app ("open Valorant", "launch Blender"), plus Cursor, VS Code, Notepad and the others in `approved_apps`. `blocked_shortcuts` (ShutDown, MinGW Installer, old JARVIS) are excluded.
- **Your files and folders:** "open my GenCV", "open the DSA folder", "open cdp 48 pdf". Spidey searches Desktop, Documents and Downloads, **always asks before opening**, and says "which one?" when there are several matches (answer "first", "second", or a name). Saying *folder*, *pdf*, *word*, *photo* and so on filters by type. It never runs `.exe`, `.bat`, scripts or installers this way.
- **Things it can do** (explicit tools, never a raw shell): tell the time, open approved apps and websites, set and list reminders, search approved folders, run allowlisted commands, summarize the conversation. Unapproved sites and commands marked `confirm` need a spoken or on-screen **yes**. Edit the allowlists in `data/settings.json`.

## Measured on this laptop (i5-1340P, 16 GB, Iris Xe, no GPU acceleration)

Real numbers from `scripts/voice_loop_check.py` and the running server, not estimates:

| Step | Time |
|---|---|
| End-of-turn detection (configurable) | 0.70 s of silence |
| Speech recognition, Whisper base.en (2–5 s utterance) | 0.43–0.74 s |
| Command replies (time, open app…), routed locally | **first audio 0.55–1.14 s** after end of turn |
| Chat replies, llama3.2:3b: first token | 0.27–0.65 s |
| Chat replies: first spoken phrase ready | 1.3–2.2 s |
| Chat replies: **first audio** | **2.4–3.4 s** after end of turn (+0.7 s silence window) |
| Piper synthesis (medium voices) | 0.07–0.3 s per sentence (about 25× faster than real time) |
| Kokoro synthesis (optional voice) | 0.7–1.0 s per short sentence idle, ~2 s while the LLM is still generating |
| Model load at start (Whisper + Piper + VAD + warm LLM) | 3.6–9 s |

| Memory | |
|---|---|
| Backend (Python, Whisper + Piper + VAD loaded) | 444–518 MB |
| Ollama with llama3.2:3b loaded | 2.6 GB |
| **Total** | **about 3.1 GB** |

## Voices

All voices are free, open source and run offline on the CPU. Pick one in **Settings → Voice**; speed is the *speaking rate* slider.

| Voice | Sound | Short sentence (i5-1340P) | Notes |
|---|---|---|---|
| `en_GB-northern_english_male-medium` | natural British male | 0.07 s | **Default.** Fast enough to never delay a reply |
| `en_US-amy-medium` | US female | 0.10 s | |
| `en_US-ryan-high` | natural US male | 0.4 s | Bigger model, slightly smoother |
| `en_GB-alan-medium`, `en_US-ryan-medium` | older defaults | 0.1 s | More robotic |
| `kokoro:am_michael`, `kokoro:af_heart` (+ 26 more `kokoro:*`) | most human-like (Kokoro-82M) | 0.7–1.0 s idle, ~2 s during LLM output | Adds 1–2 s before Spidey starts talking on this CPU; ~360 MB extra RAM |

Kokoro needs `models/kokoro/kokoro-v1.0.int8.onnx` and `voices-v1.0.bin` (downloaded by `install.ps1`, ~120 MB). If they are missing or fail to load, Spidey falls back to the Piper default automatically. Kokoro voice names: `a`/`b` = American/British, `f`/`m` = female/male. Hear the voices: `data/voice_demo.wav` (default), `data/voice_demo_kokoro.wav`, `data/voice_demo_old.wav`.

Before speaking, text is lightly normalised so symbols aren't read aloud: `32°C` → "32 degrees", `km/h` → "kilometres per hour", `&` → "and", `₹500` → "500 rupees", `80%` → "80 percent"; URLs, emoji and markdown are dropped.

## Feature matrix

| Feature | Status | Offline? | Notes |
|---|---|---|---|
| Mic capture, VAD, turn detection | ✅ Implemented | ✅ | Auto-reconnects if the mic drops |
| Speech recognition | ✅ | ✅ | tiny / base / small `.en` selectable |
| Local LLM conversation + context memory | ✅ | ✅ | Last 12 turns; saved to `data/conversations/*.jsonl` |
| Text-to-speech, voice and speed selection | ✅ | ✅ | 5 Piper voices + optional Kokoro voices; symbols, units, URLs and emoji are cleaned before speaking |
| Push-to-talk, hands-free, wake word | ✅ | ✅ | Wake word checked on the local transcript, nothing uploaded |
| Barge-in (interrupt by speaking) | ✅ | ✅ | Uses measured echo coupling; headphones are most reliable |
| Unfinished-sentence hold ("so I was thinking and…") | ✅ | ✅ | Waits 0.9 s for you to continue |
| Clarification on unclear speech | ✅ | ✅ | Low-confidence short phrases get "say that again?" |
| Tools with allowlists + confirmation | ✅ | ✅ | Common commands are recognized instantly without the LLM |
| Reminders | ✅ | ✅ | Spoken and shown in the dashboard when due |
| Dashboard (status, transcript, settings, history, latency) | ✅ | ✅ | Served by the backend; works without internet |
| **Test-call simulator** | ✅ **Simulated** | ✅ | Clearly labelled SIMULATED; real G.711 audio path, VAD, STT, handoff |
| **SIP over local network** (call Spidey from your phone) | ✅ Implemented, off by default | ✅ | Direct-IP SIP (UDP+TCP) + RTP G.711 for a free softphone (Linphone, Zoiper) on a hotspot. Tested with a simulated Linphone over loopback and over the laptop's real LAN IP; **not yet tested with a real phone** |
| Human handoff | ✅ | ✅ | AI pauses; your typed replies are spoken into the call |
| Call status, duration, errors, history | ✅ | ✅ | |
| **Real phone calls to ordinary numbers (PSTN)** | ❌ **Disabled** | — | See below |
| Emergency stop | ✅ | ✅ | |

## Phone calls, honestly

**Ordinary phone numbers cannot be called or answered at ₹0.** Reaching the phone network needs a carrier or SIP trunk (Twilio, Exotel, Plivo, a VoIP DID and so on). All of them bill per minute and/or per number, and their "free trials" need a card or convert to paid. A SIP account alone does not include free calls to phone numbers. So:

- The **cost-safety gate** (`backend/jarvis/telephony/gate.py`) refuses any destination that looks like a phone number, refuses domains on SIP (they may reach paid services), only allows private or loopback IPs, needs your consent for every call, and fails closed. `real_calls_enabled` is `false`, and even setting it `true` does nothing without a provider marked as verified zero-cost. None exists in this project.
- **What does work for free:** your phone calls the laptop directly with a free softphone app over a network you control (see the next section). Spidey answers, says it's an AI, and talks over real SIP/RTP audio. Spidey can also call a softphone at `sip:anything@<phone-IP>:5060`.
- **Asterisk:** evaluated. It has no native Windows build (it needs WSL2 or Docker), and it still needs a paid trunk for real numbers. The built-in SIP endpoint covers local SIP-to-SIP testing without it. The `TelephonyProvider` interface (`telephony/base.py`) is where an Asterisk/ARI adapter would plug in.
- By default phone callers **cannot use tools**. A call gets its own conversation with no computer control, and Spidey will not claim to have done things it can't do. Optional **owner mode** (below) changes that for your own phone only.

### Call Spidey from your phone (₹0, no account, no internet)

**Why a hotspot:** hostel/campus/cafe Wi-Fi (e.g. *LPU Hostels-5G*) almost always has *client isolation*: devices on it can't reach each other. Windows also treats it as a Public network. So Spidey deliberately ignores it, and calls go over a personal hotspot instead. The call audio stays between the two devices and uses no mobile data.

**On the laptop (once):**

1. Run the firewall setup once: right-click `scripts\setup-phone.ps1` → *Run with PowerShell* (or `powershell -ExecutionPolicy Bypass -File scripts\setup-phone.ps1`). It shows the laptop's IPs, then asks for admin permission (UAC) and adds inbound rules for Spidey's Python: UDP 5062 + RTP UDP 40000–40101 + TCP 5062. The rules cover **Private networks only, from the local subnet only**. If the Windows hotspot adapter counts as Public, it offers an extra rule limited to `192.168.137.0/24`. To undo, run it with `-Remove`. Use `-InfoOnly` to only print addresses. (The rules name both `.venv\Scripts\python.exe` and the base `Python312\python.exe`. The venv exe is only a launcher, and the base interpreter is the process that actually owns the sockets.)
2. Turn on **Windows Mobile Hotspot**: Settings → Network & internet → Mobile hotspot → share *Wi-Fi* over *Wi-Fi* → note the name and password → **On**. For phones that join it, the laptop is **192.168.137.1**.
3. In the dashboard (`http://127.0.0.1:8600/#calls`), open the **Call Spidey from your phone** card and tick **Enabled**. The card shows the address to dial for each network (plus a QR code), whether the listener and firewall rules are in place, and which networks may call.

**On the phone (Android or iPhone):**

1. Join the laptop's hotspot Wi-Fi.
2. Install **Linphone** (free, Play Store / App Store). No account is needed: skip the sign-in, open the dialer, type `sip:jarvis@192.168.137.1:5062` and call. You can scan the card's QR code with the camera to copy the address instead of typing it.
3. If your Linphone version insists on an account: *Use a SIP account* / *Third-party SIP account*, username `phone`, any password, domain `192.168.137.1:5062`, transport **UDP**. Spidey accepts this registration locally. Then dial `jarvis`.
4. Keep Linphone's **Media encryption = None** and the **PCMU/PCMA** codecs on (both are defaults). Spidey rejects SRTP-only offers with a 488 that says why.
5. **Zoiper** (free) also works: add an account with username `phone`, any password, domain `192.168.137.1:5062`, skip the provider search, then dial `jarvis`.

**If Mobile hotspot is greyed out** (some Wi-Fi cards can't host while connected), do the reverse: connect the laptop to the **phone's** hotspot and choose *Private network* when Windows asks. In the card, click *Allow this network…* next to that network (iPhone hotspots are `172.20.10.0/28`), then dial the address shown. The laptop's other internet traffic will then use the phone's data.

**Who can call:** `telephony.sip_allowed_ips` holds single IPs or CIDR subnets. The default is only `192.168.137.0/24` (Windows hotspot clients), plus this PC. Public/internet ranges such as `0.0.0.0/0` are refused. Packets from any other address get no reply at all; the card lists them under *Recently ignored*.

**What the SIP endpoint handles** (`backend/jarvis/telephony/sip_lan.py`): INVITE with opus/speex/PCMU/PCMA/G.729/telephone-event offers (picks PCMU, then PCMA). Via `rport`/`received`. 100 Trying → 180 Ringing → 200 OK, with the 200 retransmitted until the ACK arrives. INVITE retransmissions. re-INVITE/UPDATE (hold/resume). OPTIONS keepalives. CANCEL (487). BYE from either side. 481 for unknown dialogs. REGISTER/INFO/MESSAGE. SIP over UDP and TCP. Symmetric RTP: the phone's real media address is learnt from its first packet. Steady 20 ms PCMU/PCMA packets out (silence between sentences). DTMF and comfort-noise payloads are ignored. Not supported: SRTP/ZRTP, ICE/STUN, TLS, video, NAT traversal.

**Owner mode (optional):** set *Owner phone IPs* in the card (setting `telephony.owner_ips`, single IPs or subnets). Calls from those addresses get the full local toolset (open apps and websites, reminders and so on) with the same confirmations as on the laptop. The AI greeting stays. Default is empty, which means talk-only. Only use it on a hotspot only you are on, because another device on a shared network could fake the address.

## Privacy and security

- The backend binds to `127.0.0.1` only and rejects foreign `Host`/`Origin` headers (blocks other websites and DNS-rebinding). Optional `JARVIS_API_TOKEN` in `.env`.
- The SIP endpoint is off by default. When on, it drops packets from any IP outside the allowlist (default: only the Windows Mobile Hotspot subnet `192.168.137.0/24` and this PC). The firewall rules from `setup-phone.ps1` open it on Private networks only.
- Everything (settings, transcripts, reminders) stays in `data/`, which is git-ignored. Turn off transcript saving in Settings.
- No voice cloning. Spidey identifies itself as an AI on calls and when asked.

## Tests

```powershell
.venv\Scripts\python -m pytest -q              # 172 tests (about 45 s), includes real-model offline tests
.venv\Scripts\python -m pytest -q -m "not slow" # fast suite, no models needed
.venv\Scripts\python scripts\voice_loop_check.py [--loud]   # end-to-end voice loop with recorded speech + timings
```

The tests cover: G.711 μ-law/A-law against the reference implementation; resampling; every cost-gate rule; tool allowlists and confirmations; the intent router; sentence chunking; interruption memory; wake word; barge-in; push-to-talk; the simulated call lifecycle and handoff; a real SIP/RTP call over loopback (including Spidey's spoken greeting); API host/origin/token checks; the emergency stop; and Whisper, Piper, VAD and Ollama running offline.

## Known limitations

- **llama3.2:3b is small.** Chat is fluent but sometimes wrong on facts. Try `qwen2.5:3b` or a 7B model if you can accept slower replies (a 7B model roughly halves speed on this CPU).
- Laptop speakers + mic without headphones: barge-in relies on the Intel mic array's echo cancellation (measured coupling about 0.001 on this laptop). Run `calibrate_echo.py` if Spidey interrupts itself or ignores you.
- The live voice tests use recorded speech fed through the full pipeline. Real-microphone accuracy depends on your room and voice.
- SIP LAN: no NAT traversal, encryption or authentication; it's meant for a hotspot you control. It has not been tried with a real phone yet. The tests use a simulated Linphone (realistic INVITE/SDP/RTP) over loopback and over the laptop's real Wi-Fi IP.

## Troubleshooting

| Problem | Fix |
|---|---|
| Dashboard says *LLM loading* forever | Start Ollama (`ollama serve`, or open the Ollama app) and check `ollama list` shows `llama3.2:3b` |
| Spidey doesn't hear you | Settings → Microphone: pick the right device; Windows Settings → Privacy → Microphone → allow desktop apps; watch the green level bar |
| Spidey hears itself / interrupts itself | Use headphones, or run `scripts\calibrate_echo.py`, or untick *Let me interrupt Spidey* |
| Ignores you in hands-free | Start with "Jarvis", or switch activation to *Any speech* |
| Slow first reply | The model loads into RAM on first use (about 4 s); it stays loaded for 30 min |
| Port 8600 in use | Something else is running there; close it, or change `PORT` in `backend/jarvis/server.py` and `vite.config.ts` |
| Phone/softphone can't reach Spidey | Is the phone on the laptop's Mobile hotspot (not hostel Wi-Fi)? Is SIP *Enabled* in Calls? Did you run `scripts\setup-phone.ps1` once? Is the phone's network in *Allowed networks*? Is the dialled address shown in the card? Does *Recently ignored* list your phone's IP? If so, allow it |
| Call connects but there's no audio | In Linphone set Media encryption = None and enable PCMU/PCMA; re-run `setup-phone.ps1` (RTP ports 40000–40101 must be allowed) |
