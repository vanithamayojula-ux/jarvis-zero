import warnings

import numpy as np
import pytest

from jarvis.telephony.base import CallKind
from jarvis.telephony.codec import alaw_decode, alaw_encode, from_phone, resample, to_phone, ulaw_decode, ulaw_encode
from jarvis.telephony.gate import CostSafetyGate
from jarvis.telephony.mock import MockProvider
from jarvis.telephony.sip_lan import SipLanProvider

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import audioop  # reference G.711 implementation (stdlib, Python ≤ 3.12)


def test_ulaw_matches_reference():
    pcm = np.linspace(-32768, 32767, 4000).astype(np.int16)
    assert ulaw_encode(pcm) == audioop.lin2ulaw(pcm.tobytes(), 2)
    ref = np.frombuffer(audioop.ulaw2lin(ulaw_encode(pcm), 2), np.int16)
    assert np.array_equal(ulaw_decode(ulaw_encode(pcm)), ref)


def test_alaw_matches_reference():
    pcm = np.linspace(-32768, 32767, 4000).astype(np.int16)
    assert alaw_encode(pcm) == audioop.lin2alaw(pcm.tobytes(), 2)
    ref = np.frombuffer(audioop.alaw2lin(alaw_encode(pcm), 2), np.int16)
    assert np.array_equal(alaw_decode(alaw_encode(pcm)), ref)


@pytest.mark.parametrize("codec", ["PCMU", "PCMA"])
def test_phone_roundtrip_keeps_speech_band(codec):
    t = np.arange(16000) / 16000
    pcm = (np.sin(2 * np.pi * 440 * t) * 10000).astype(np.int16)
    payload = to_phone(pcm, 16000, codec)
    assert len(payload) == 8000  # 1 s at 8 kHz, one byte per sample
    back = from_phone(payload, codec, 16000)
    snr = 10 * np.log10(np.sum(pcm[200:-200].astype(float) ** 2) / np.sum((pcm[200:-200] - back[200:-200]).astype(float) ** 2))
    assert snr > 20


def test_resample_lengths():
    assert len(resample(np.zeros(22050, np.int16), 22050, 8000)) == 8000
    assert len(resample(np.zeros(8000, np.int16), 8000, 16000)) == 16000


TEL = {"real_calls_enabled": False, "sip_lan_enabled": True}


def test_gate_needs_consent():
    assert not CostSafetyGate().check_outbound(MockProvider(), "+911234567890", TEL, False).allowed


def test_gate_simulated_is_allowed():
    d = CostSafetyGate().check_outbound(MockProvider(), "+911234567890", TEL, True)
    assert d.allowed and "Simulated" in d.reason


def test_gate_blocks_phone_numbers_on_real_providers():
    sip = SipLanProvider.__new__(SipLanProvider)  # attributes only, no sockets
    for number in ["+91 98765 43210", "9876543210", "(555) 123-4567"]:
        d = CostSafetyGate().check_outbound(sip, number, TEL, True)
        assert not d.allowed
    d = CostSafetyGate().check_outbound(sip, "+919876543210", {**TEL, "real_calls_enabled": True}, True)
    assert not d.allowed, "even with the flag on, an unverified provider must not dial PSTN"


def test_gate_sip_lan_rules():
    sip = SipLanProvider.__new__(SipLanProvider)
    gate = CostSafetyGate()
    assert gate.check_outbound(sip, "sip:phone@192.168.1.20", TEL, True).allowed
    assert gate.check_outbound(sip, "sip:me@127.0.0.1:5070", TEL, True).allowed
    assert not gate.check_outbound(sip, "sip:me@8.8.8.8", TEL, True).allowed
    assert not gate.check_outbound(sip, "sip:me@sip.provider.com", TEL, True).allowed
    assert not gate.check_outbound(sip, "sip:phone@192.168.1.20", {**TEL, "sip_lan_enabled": False}, True).allowed


def test_emergency_lock_blocks_everything():
    gate = CostSafetyGate()
    gate.emergency_lock()
    assert not gate.check_outbound(MockProvider(), "sim", TEL, True).allowed
    gate.unlock()
    assert gate.check_outbound(MockProvider(), "sim", TEL, True).allowed


def test_provider_flags():
    assert MockProvider.kind == CallKind.SIMULATED and not MockProvider.reaches_pstn
    assert SipLanProvider.kind == CallKind.SIP_LAN and not SipLanProvider.reaches_pstn
