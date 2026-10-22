"""Mock provider for development and the dashboard's test-call simulator. Never touches a network."""
from __future__ import annotations

import asyncio

from .base import CallKind, CallSession, CallState, TelephonyProvider


class MockProvider(TelephonyProvider):
    name = "mock"
    kind = CallKind.SIMULATED

    def __init__(self, ring_seconds: float = 0.5):
        super().__init__()
        self.ring_seconds = ring_seconds
        self.sent: dict[str, list[bytes]] = {}    # call id -> audio JARVIS "spoke" into the call
        self.running = False

    async def start(self) -> None:
        self.running = True

    async def stop(self) -> None:
        self.running = False

    async def dial(self, call: CallSession) -> None:
        call.state = CallState.RINGING
        self.on_state(call)
        await asyncio.sleep(self.ring_seconds)
        if call.state == CallState.RINGING:
            call.state = CallState.ACTIVE
            self.on_state(call)

    async def answer(self, call: CallSession) -> None:
        call.state = CallState.ACTIVE
        self.on_state(call)

    async def hangup(self, call: CallSession) -> None:
        call.state = CallState.ENDED
        self.on_state(call)

    async def send_audio(self, call: CallSession, payload: bytes) -> None:
        self.sent.setdefault(call.id, []).append(payload)

    # --- simulator controls ---
    def simulate_incoming(self, caller: str) -> CallSession:
        call = CallSession(direction="inbound", remote=caller, provider=self.name, kind=self.kind, state=CallState.RINGING)
        self.on_incoming(call)
        return call

    def simulate_caller_audio(self, call: CallSession, payload: bytes) -> None:
        self.on_audio(call, payload)
