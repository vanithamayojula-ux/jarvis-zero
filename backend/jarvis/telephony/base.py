"""Telephony contracts: call sessions, states and the provider interface."""
from __future__ import annotations

import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Callable


class CallState(str, Enum):
    DIALING = "dialing"
    RINGING = "ringing"
    ACTIVE = "active"
    HANDOFF = "handoff"        # AI paused, a human is handling the call
    ENDED = "ended"
    FAILED = "failed"
    BLOCKED = "blocked"        # stopped by the cost-safety gate


class CallKind(str, Enum):
    SIMULATED = "simulated"    # mock provider: no network, no phone line
    SIP_LAN = "sip_lan"        # real SIP audio over your own network, no phone numbers
    PSTN = "pstn"              # ordinary phone numbers — needs a carrier (not available at ₹0)


@dataclass
class CallSession:
    direction: str                          # inbound | outbound
    remote: str
    provider: str
    kind: CallKind
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    state: CallState = CallState.DIALING
    created_at: float = field(default_factory=time.time)
    answered_at: float | None = None
    ended_at: float | None = None
    error: str = ""
    codec: str = "PCMU"
    transcript: list[dict] = field(default_factory=list)
    ai_active: bool = True
    peer_ip: str = ""                       # network address the call came from / went to
    owner: bool = False                     # owner mode: caller IP is in telephony.owner_ips -> local tools

    @property
    def duration(self) -> float:
        if not self.answered_at:
            return 0.0
        return round((self.ended_at or time.time()) - self.answered_at, 1)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(state=self.state.value, kind=self.kind.value, duration=self.duration,
                 label={"simulated": "SIMULATED CALL — not a phone call",
                        "sip_lan": "SIP over local network (no phone number)",
                        "pstn": "REAL TELEPHONE CALL"}[self.kind.value])
        return d


class TelephonyProvider(ABC):
    """A provider moves audio between a call and JARVIS. 8 kHz G.711 payloads on the wire."""

    name: str = "base"
    kind: CallKind = CallKind.SIMULATED
    reaches_pstn: bool = False              # can it dial ordinary phone numbers?
    zero_cost_verified: bool = False        # has every cost been verified to be zero?

    def __init__(self):
        self.on_incoming: Callable[[CallSession], None] = lambda call: None
        self.on_audio: Callable[[CallSession, bytes], None] = lambda call, payload: None
        self.on_state: Callable[[CallSession], None] = lambda call: None

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def dial(self, call: CallSession) -> None: ...

    @abstractmethod
    async def answer(self, call: CallSession) -> None: ...

    @abstractmethod
    async def hangup(self, call: CallSession) -> None: ...

    @abstractmethod
    async def send_audio(self, call: CallSession, payload: bytes) -> None:
        """Send G.711 payload (any length; providers packetize into 20 ms frames)."""

    def status(self) -> dict:
        return {"name": self.name, "kind": self.kind.value, "reaches_pstn": self.reaches_pstn,
                "zero_cost_verified": self.zero_cost_verified}
