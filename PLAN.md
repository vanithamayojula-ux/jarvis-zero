# JARVIS Zero — implementation plan

Budget ₹0 · local-first · offline after install · Windows 11, i5-1340P, 16 GB RAM, Iris Xe (no CUDA).

## Phase 1 inspection (2026-10-21)

| Item | Found |
|---|---|
| Python | 3.12.10 |
| Node / npm | 24.21.0 / 11.19.0 |
| Git | 2.55 |
| Disk | 30 GB free on C: (94% used) |
| RAM | 15.6 GB total, ~6 GB free while other apps run |
| Ollama | 0.40.2 at 127.0.0.1:11434 · llama3.2:3b, qwen2.5-coder:3b/7b |
| Local LLM speed | llama3.2:3b warm: ~16 tok/s, 55-token prompt in 0.07 s; cold load ~4.3 s |
| Whisper cache | faster-whisper tiny.en / base.en / medium.en already downloaded |
| Mic / speaker | Intel Smart Sound mic array (default in), Realtek speaker (default out) |
| Ports | 11434 (Ollama), 8790 (call-agent) in use → JARVIS Zero uses 8600 (API) and 5173 (dashboard dev) |

## Architecture

```
backend/jarvis/
  audio/      capture (sounddevice 16 kHz mono frames) · playback (interruptible)
  vad.py      Silero VAD (ONNX, CPU)
  stt.py      faster-whisper (base.en, int8, CPU)
  llm.py      Ollama streaming chat (llama3.2:3b, kept warm)
  tts.py      Piper (local ONNX voice)
  engine.py   state machine: idle → listening → thinking → speaking; barge-in; turn detection
  tools/      explicit tool registry with allowlists and a confirmation gate
  telephony/  provider interface · call lifecycle · G.711 codecs · mock provider · cost-safety gate
  server.py   FastAPI on 127.0.0.1 + WebSocket event stream
frontend/     React + TypeScript dashboard (Vite)
```

Each component sits behind a small interface (`VAD`, `STT`, `LLM`, `TTS`, `TelephonyProvider`) so it can be swapped.

## Checklist

- [x] Phase 1 inspect + plan
- [x] Phase 2 local voice loop (capture → VAD → STT → LLM → TTS → playback, interruption)
- [x] Phase 3 natural conversation, latency measurement
- [x] Phase 4 tools with allowlists + confirmation
- [x] Phase 5 telephony adapter, mock provider, cost gate, SIP evaluation (PSTN disabled: no ₹0 route; SIP LAN loopback-tested, not yet with a real phone)
- [x] Phase 6 dashboard
- [x] Phase 7 security/privacy
- [x] Phase 8 tests + measured RAM/latency
- [x] Phase 9 install script, README, feature matrix
