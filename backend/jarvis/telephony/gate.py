"""Cost-safety gate. Every outbound call passes through here; it fails closed."""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .base import CallKind, TelephonyProvider

PSTN_NUMBER = re.compile(r"^\+?[\d\s().-]{5,}$")


@dataclass
class GateDecision:
    allowed: bool
    reason: str


class CostSafetyGate:
    def __init__(self):
        self.locked = False                 # emergency stop: no outbound calls until restart/unlock

    def emergency_lock(self) -> None:
        self.locked = True

    def unlock(self) -> None:
        self.locked = False

    def check_outbound(self, provider: TelephonyProvider, destination: str, settings_tel: dict,
                       user_consented: bool) -> GateDecision:
        if self.locked:
            return GateDecision(False, "Emergency stop is active: outbound calling is disabled.")
        if not user_consented:
            return GateDecision(False, "Outbound calls need your explicit consent for each call.")
        dest = destination.strip()
        looks_pstn = bool(PSTN_NUMBER.match(dest)) and not dest.lower().startswith("sip:")

        if provider.kind == CallKind.SIMULATED:
            return GateDecision(True, "Simulated call: no network, no phone line, no cost.")

        if looks_pstn or provider.kind == CallKind.PSTN:
            # Real phone numbers are never dialled at ₹0: there is no verified free carrier route.
            if not settings_tel.get("real_calls_enabled"):
                return GateDecision(False, "Real phone calls are disabled. No verified zero-cost route to "
                                           "ordinary phone numbers exists, so Spidey will not dial them.")
            if not (provider.reaches_pstn and provider.zero_cost_verified):
                return GateDecision(False, f"Provider '{provider.name}' is not verified as zero-cost for phone numbers.")
            return GateDecision(False, "Blocked: PSTN calling requires a manually verified provider; none is configured.")

        if provider.kind == CallKind.SIP_LAN:
            if not settings_tel.get("sip_lan_enabled"):
                return GateDecision(False, "SIP over LAN is disabled in settings.")
            host = _sip_host(dest)
            if not host:
                return GateDecision(False, "Use a SIP address like sip:phone@192.168.1.20.")
            try:
                ip = ipaddress.ip_address(host)
            except ValueError:
                return GateDecision(False, "SIP LAN calls must use a local IP address, not a domain (domains may reach paid services).")
            if not (ip.is_private or ip.is_loopback):
                return GateDecision(False, "Only private/loopback addresses are allowed for SIP LAN calls.")
            return GateDecision(True, "Direct SIP call on your own network: no carrier, no cost.")

        return GateDecision(False, "Unknown provider type.")


def _sip_host(uri: str) -> str:
    m = re.match(r"^sip:(?:[^@]+@)?([^:;>]+)", uri.strip(), re.I)
    return m.group(1) if m else ""
