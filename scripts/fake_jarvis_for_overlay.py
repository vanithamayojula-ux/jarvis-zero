r"""Fake JARVIS on 127.0.0.1:8611 for testing the floating bot (scripts/bot_overlay.py) without the real server.

  .venv\Scripts\python.exe scripts\fake_jarvis_for_overlay.py
  set JARVIS_OVERLAY_URL=http://127.0.0.1:8611
  .venv\Scripts\python.exe scripts\bot_overlay.py --test 9 --demo-chat "hey"
Answers /api/status, /api/text and /ws with a scripted reply.
"""
import asyncio
import sys

import uvicorn
from fastapi import FastAPI, WebSocket
from pydantic import BaseModel

app = FastAPI()
state = {"state": "idle", "io_mode": "chat" if "--chat" in sys.argv else "voice"}
subs: list[asyncio.Queue] = []
got: list[str] = []


class TextIn(BaseModel):
    text: str


def emit(ev):
    for q in subs:
        q.put_nowait(ev)


@app.get("/api/status")
async def status():
    return {"engine": {"state": state["state"], "io_mode": state["io_mode"], "conversation_id": "demo"}}


@app.get("/api/conversations/{cid}")
async def conversation(cid: str):
    return {"id": cid, "messages": [{"role": "user", "text": "what's the weather like?"},
                                    {"role": "assistant", "text": "Sunny and 31 degrees in Hyderabad, says weather.com."}]}


@app.post("/api/conversation/new")
async def new_conversation():
    emit({"type": "conversation", "id": "new"})
    return {"id": "new"}


@app.post("/api/overlay/panel")
async def panel():
    emit({"type": "show_panel"})
    return {"ok": True}


@app.post("/api/text")
async def text(body: TextIn):
    got.append(body.text)
    print("GOT", body.text, flush=True)

    async def turn():
        emit({"type": "transcript", "role": "user", "text": body.text})
        if "highlight demo" in body.text.lower():
            # the "this is where I will click" ring: a 300x80 outline near the top left of the screen for half a second
            emit({"type": "highlight", "rect": [200, 200, 500, 280], "label": "Save", "ttl": 0.55})
            await say(["That's where I'd click."])
            return
        if "on my screen" in body.text.lower() or "stop looking" in body.text.lower() or "look at the screen" in body.text.lower():
            # like the real thing: "what's on my screen" shows "looking at Notepad" for 3 seconds; the toggles flip the setting
            if "on my screen" in body.text.lower():
                emit({"type": "looking", "app": "Notepad", "expires_in": 3})
                lines = ["In Notepad: Hello JARVIS test 123."]
            else:
                state["screen_reading"] = "stop" not in body.text.lower()
                lines = ["Okay, I'll read a window again when you ask." if state["screen_reading"] else
                         "Okay, I've stopped looking at the screen."]
            await say(lines)
            return
        state["state"] = "thinking"
        emit({"type": "state", "state": "thinking"})
        await asyncio.sleep(0.8)
        state["state"] = "speaking"
        emit({"type": "state", "state": "speaking"})
        for s in ["Pretty good, thanks!", "Just hanging out on your desktop. What are you up to?"]:
            emit({"type": "transcript", "role": "assistant", "text": s})
            await asyncio.sleep(0.6)
        state["state"] = "idle"
        emit({"type": "state", "state": "idle"})
    asyncio.create_task(turn())
    return {"ok": True}


@app.get("/api/overlay")
async def overlay():
    return {"cloud": True, "cloud_reason": "", "io_mode": state["io_mode"], "web": True,
            "media": {"playing": False, "title": ""}, "killed": state.get("killed", False),
            "kill_hotkey": "ctrl+alt+backspace", "looking": None,
            "screen_reading": state.get("screen_reading", True)}


@app.post("/api/emergency-stop")
async def emergency_stop():
    state["killed"] = True
    emit({"type": "kill"})
    return {"ok": True}


@app.post("/api/resume")
async def resume():
    state["killed"] = False
    emit({"type": "resume"})
    return {"ok": True}


class SettingsIn(BaseModel):
    changes: dict


@app.patch("/api/settings")
async def settings(body: SettingsIn):
    print("GOT settings", body.changes, flush=True)
    if body.changes.get("io_mode") in ("chat", "voice") and body.changes["io_mode"] != state["io_mode"]:
        state["io_mode"] = body.changes["io_mode"]
        emit({"type": "io_mode", "mode": state["io_mode"]})
    return {"io_mode": state["io_mode"]}


async def say(lines):
    state["state"] = "speaking"
    emit({"type": "state", "state": "speaking"})
    for s in lines:
        emit({"type": "transcript", "role": "assistant", "text": s})
        await asyncio.sleep(0.6)
    state["state"] = "idle"
    emit({"type": "state", "state": "idle"})


@app.post("/api/confirm/{answer}")
async def confirm(answer: str):
    print("GOT confirm", answer, flush=True)
    asyncio.create_task(say(["Sent." if answer == "yes" else "Okay, I won't send it."]))
    return {"ok": True}


class FileIn(BaseModel):
    name: str
    text: str


@app.post("/api/summarize")
async def summarize(body: FileIn):
    print("GOT summarize", body.name, len(body.text), flush=True)
    asyncio.create_task(say([f"{body.name} is a short note with {len(body.text.split())} words."]))
    return {"ok": True}


@app.post("/api/listen/once")
async def listen_once():
    print("GOT listen/once", flush=True)
    if state["io_mode"] == "chat":
        emit({"type": "open_chat"})
        return {"state": "idle", "listening": False}

    async def turn():
        state["state"] = "listening"
        emit({"type": "state", "state": "listening"})
        await asyncio.sleep(2.0)
        state["state"] = "idle"
        emit({"type": "state", "state": "idle"})
    asyncio.create_task(turn())
    return {"state": "listening", "listening": True}


@app.post("/api/interrupt")
async def interrupt():
    print("GOT interrupt", flush=True)
    state["state"] = "idle"
    emit({"type": "state", "state": "idle"})
    return {"ok": True}


@app.websocket("/ws")
async def ws(websocket: WebSocket):
    await websocket.accept()
    q: asyncio.Queue = asyncio.Queue()
    subs.append(q)
    await websocket.send_json({"type": "hello", "status": {"state": state["state"], "io_mode": state["io_mode"]}})
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), 0.25)
            except asyncio.TimeoutError:
                ev = {"type": "level", "mic": 0, "state": state["state"]}
            await websocket.send_json(ev)
    except Exception:
        subs.remove(q)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8611, log_level="warning")
