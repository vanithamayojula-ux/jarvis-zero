"""Fake JARVIS on 127.0.0.1:8611 for testing the floating bot (scripts/bot_overlay.py) without the real server.

  .venv\Scripts\python.exe scripts\fake_jarvis_for_overlay.py
  set JARVIS_OVERLAY_URL=http://127.0.0.1:8611
  .venv\Scripts\python.exe scripts\bot_overlay.py --test 9 --demo-chat "hey"
Answers /api/status, /api/text and /ws with a scripted reply.
"""
import asyncio

import uvicorn
from fastapi import FastAPI, WebSocket
from pydantic import BaseModel

app = FastAPI()
state = {"state": "idle"}
subs: list[asyncio.Queue] = []
got: list[str] = []


class TextIn(BaseModel):
    text: str


def emit(ev):
    for q in subs:
        q.put_nowait(ev)


@app.get("/api/status")
async def status():
    return {"engine": {"state": state["state"]}}


@app.post("/api/text")
async def text(body: TextIn):
    got.append(body.text)
    print("GOT", body.text, flush=True)

    async def turn():
        emit({"type": "transcript", "role": "user", "text": body.text})
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


@app.post("/api/listen/once")
async def listen_once():
    print("GOT listen/once", flush=True)

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
    await websocket.send_json({"type": "hello", "status": {"state": state["state"]}})
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
