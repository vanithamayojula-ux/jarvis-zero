import asyncio
import socket
import struct
import threading
import time
import uuid

import numpy as np
import pytest

from conftest import EnergyVAD, FakeLLM, FakeSTT, FakeTTS, tone
from jarvis.engine import Engine
from jarvis.telephony.base import CallState
from jarvis.telephony.manager import TelephonyManager
from jarvis.telephony.sip_lan import SipLanProvider
from jarvis.testing import ScriptedMic, SilentSpeaker


async def run_engine(settings, script, stt_texts, llm_replies, seconds, setup=None):
    ScriptedMic.script = script
    eng = Engine(settings, EnergyVAD(), FakeSTT(stt_texts), FakeLLM(llm_replies), FakeTTS(), None,
                 mic_factory=ScriptedMic, speaker_factory=SilentSpeaker)
    await eng.start()
    q = eng.subscribe()
    if setup:
        setup(eng)
    eng.start_listening()
    events = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        try:
            events.append(await asyncio.wait_for(q.get(), 0.1))
        except asyncio.TimeoutError:
            pass
    await eng.shutdown()
    return eng, events


def test_hands_free_wake_word(settings):
    settings.update({"mode": "hands_free", "activation": "wake_word", "silence_ms": 400})
    script = [("silence", 0.3), ("say", tone(0.6)), ("silence", 0.8), ("say", tone(0.6)), ("silence", 1.5)]
    eng, ev = asyncio.run(run_engine(settings, script, ["talking about dinner", "Jarvis, hello there"],
                                     ["Hi. Good to hear you."], 4.5))
    assert any(e["type"] == "ignored" for e in ev)
    users = [e["text"] for e in ev if e["type"] == "transcript" and e["role"] == "user"]
    assert users == ["hello there"]
    assert any(e["type"] == "latency" for e in ev)


def test_barge_in_stops_speech(settings):
    settings.update({"mode": "hands_free", "activation": "voice", "silence_ms": 300, "echo_coupling": 0.1})
    long_reply = " ".join(f"Sentence number {i}." for i in range(6))
    # reply = 6 sentences x 0.1 s of audio, starting ~0.4 s after the first utterance; speak over it
    script = [("silence", 0.2), ("say", tone(0.5)), ("silence", 0.55), ("say", tone(0.6, 20000)), ("silence", 1.5)]
    eng, ev = asyncio.run(run_engine(settings, script, ["tell me a story", "stop"], [long_reply, "Okay."], 4.5))
    assert any(e["type"] == "interrupted" for e in ev)
    contents = [m["content"] for m in eng.conversation.history if m["role"] == "assistant"]
    assert contents[0].endswith("—")


def test_push_to_talk(settings):
    settings.update({"mode": "push_to_talk"})
    script = [("say", tone(1.0)), ("silence", 2.0)]

    async def go():
        ScriptedMic.script = script
        eng = Engine(settings, EnergyVAD(), FakeSTT(["what's up"]), FakeLLM(["Not much."]), FakeTTS(), None,
                     mic_factory=ScriptedMic, speaker_factory=SilentSpeaker)
        await eng.start()
        q = eng.subscribe()
        eng.set_ptt(True)
        await asyncio.sleep(1.0)
        eng.set_ptt(False)
        await asyncio.sleep(1.0)
        ev = []
        while not q.empty():
            ev.append(q.get_nowait())
        await eng.shutdown()
        return ev

    ev = asyncio.run(go())
    assert [e["text"] for e in ev if e["type"] == "transcript"] == ["what's up", "Not much."]


# ---------------- telephony ----------------
def make_manager(settings, replies=None, stt=None):
    events = []
    mgr = TelephonyManager(settings, FakeLLM(replies or ["Sure thing."]), stt or FakeSTT(), FakeTTS(), EnergyVAD, events.append)
    return mgr, events


def test_simulated_inbound_call_lifecycle(settings):
    async def go():
        mgr, events = make_manager(settings, ["Tomorrow works. Anything else?"], FakeSTT(["can we meet tomorrow"]))
        await mgr.start()
        call = mgr.simulate_incoming("Tester")
        await asyncio.sleep(1.2)
        rt = mgr.calls[call["id"]]
        assert rt.call.state == CallState.ACTIVE
        assert "AI assistant" in rt.call.transcript[0]["text"]  # announces itself as AI
        mgr.simulate_audio(call["id"], tone(0.8))  # through G.711 + VAD + STT
        await asyncio.sleep(0.5)
        roles = [t["role"] for t in rt.call.transcript]
        assert roles[:3] == ["jarvis", "caller", "jarvis"]
        assert mgr.providers["mock"].sent[call["id"]], "audio was sent into the call"
        await mgr.handoff(call["id"])
        await mgr.caller_said(call["id"], "hello?")
        assert rt.call.transcript[-1]["role"] == "caller"  # AI stays quiet during handoff
        await mgr.hangup(call["id"])
        status = mgr.status()
        assert status["recent_calls"][-1]["state"] == "ended"
        assert status["recent_calls"][-1]["label"].startswith("SIMULATED")
        assert status["pstn_available"] is False

    asyncio.run(go())


def test_outbound_pstn_is_blocked_and_logged(settings):
    async def go():
        mgr, _ = make_manager(settings)
        await mgr.start()
        settings.update({"telephony": {"sip_lan_enabled": True}})
        await mgr.enable_sip_lan()
        decision, call = await mgr.place_call("sip_lan", "+919876543210", consent=True)
        assert not decision.allowed and call["state"] == "blocked"
        decision, _ = await mgr.place_call("mock", "+919876543210", consent=False)
        assert not decision.allowed
        await mgr.emergency_stop()
        decision, _ = await mgr.place_call("mock", "anything", consent=True)
        assert not decision.allowed and "Emergency" in decision.reason
        await mgr.stop()

    asyncio.run(go())


def test_sip_lan_loopback_call():
    """Two real SIP endpoints on 127.0.0.1 exchange INVITE/200/ACK, RTP audio and BYE."""
    async def go():
        a = SipLanProvider("127.0.0.1", 0, advertise_ip="127.0.0.1", user="caller")
        b = SipLanProvider("127.0.0.1", 0, advertise_ip="127.0.0.1", user="jarvis")
        await a.start()
        await b.start()
        got_audio, incoming = [], []

        def on_incoming(call):
            incoming.append(call)
            asyncio.ensure_future(b.answer(call))
        b.on_incoming = on_incoming
        b.on_audio = lambda call, payload: got_audio.append(payload)
        from jarvis.telephony.base import CallKind, CallSession
        out = CallSession(direction="outbound", remote=f"sip:jarvis@127.0.0.1:{b.port}", provider="sip_lan", kind=CallKind.SIP_LAN)
        await a.dial(out)
        for _ in range(50):
            await asyncio.sleep(0.05)
            if out.state == CallState.ACTIVE and incoming and incoming[0].state == CallState.ACTIVE:
                break
        assert out.state == CallState.ACTIVE and incoming[0].state == CallState.ACTIVE
        await a.send_audio(out, b"\x7f" * 800)  # 100 ms → 5 RTP packets (the rest of the stream is silence)
        await asyncio.sleep(0.4)
        assert sum(p.count(b"\x7f") for p in got_audio) == 800
        await a.hangup(out)
        await asyncio.sleep(0.2)
        assert incoming[0].state == CallState.ENDED and out.state == CallState.ENDED
        await a.stop()
        await b.stop()

    asyncio.run(go())


def test_sip_lan_drops_unknown_hosts():
    async def go():
        b = SipLanProvider("127.0.0.1", 0, advertise_ip="127.0.0.1")
        b.allowed = {"192.168.1.50"}  # loopback not allowed for this test
        b.allow_loopback = False
        await b.start()
        seen = []
        b.on_incoming = seen.append
        phone = FakePhone("127.0.0.1")
        await phone.open()
        phone.send_sip(phone.invite("127.0.0.1", b.port), b.port)
        await asyncio.sleep(0.3)
        phone.close()
        await b.stop()
        assert seen == [] and phone.sip == [], "no answer at all to a host outside the allowlist"
        assert b.dropped.get("127.0.0.1")

    asyncio.run(go())


def test_allowlist_cidr_and_safe_default():
    from jarvis.config import DEFAULTS
    from jarvis.telephony.sip_lan import DEFAULT_ALLOWED, Allowlist
    assert DEFAULTS["telephony"]["sip_allowed_ips"] == ["192.168.137.0/24"] == DEFAULT_ALLOWED
    al = Allowlist(DEFAULT_ALLOWED)
    assert "192.168.137.23" in al and "127.0.0.1" in al
    assert "172.22.120.124" not in al and "172.22.1.9" not in al and "192.168.1.5" not in al  # never the hostel Wi-Fi
    al = Allowlist(["172.20.10.0/28", "10.0.0.7", "not-an-ip", " "])
    assert "172.20.10.1" in al and "172.20.10.16" not in al and "10.0.0.7" in al and "10.0.0.8" not in al
    assert al.invalid == ["not-an-ip"]
    assert Allowlist(["0.0.0.0/0", "8.8.8.8"]).invalid == ["0.0.0.0/0", "8.8.8.8"]  # never the internet
    assert "127.0.0.1" not in Allowlist(["10.0.0.0/8"], loopback=False)
    p = SipLanProvider("127.0.0.1", 0, allowed_ips=[])
    assert p.is_allowed("192.168.137.5") and not p.is_allowed("172.22.120.124")  # empty list -> safe default


def test_codec_negotiation_prefers_pcmu_then_pcma():
    from jarvis.telephony.sip_lan import Offer, choose_codec
    offer = Offer(linphone_sdp("10.0.0.2", 7078, "96 97 8 0 101 100"))
    assert offer.codecs == ["PCMA", "PCMU"] and choose_codec(offer.codecs) == "PCMU"
    assert offer.dtmf_pt == 100
    assert choose_codec(Offer(linphone_sdp("10.0.0.2", 7078, "96 8 101")).codecs) == "PCMA"
    assert choose_codec(Offer(linphone_sdp("10.0.0.2", 7078, "96 97 101")).codecs) is None
    assert Offer(linphone_sdp("10.0.0.2", 7078, "0", profile="RTP/SAVP")).encrypted_only


def test_qr_code_svg():
    from jarvis.telephony.qr import QR, qr_svg
    q = QR("sip:jarvis@192.168.137.1:5062")
    assert q.version == 3 and q.size == 29
    assert all(q.modules[r][c] for r, c in [(0, 0), (0, 6), (6, 0), (0, 28), (28, 0)])  # finder corners
    svg = qr_svg("sip:jarvis@192.168.137.1:5062")
    assert svg.startswith("<svg") and "path" in svg


# ---------------- a realistic Linphone-style phone (raw SIP/RTP over UDP) ----------------
def linphone_sdp(ip, port, pts="96 97 98 0 8 18 101 99 100", profile="RTP/AVP"):
    return ("v=0\r\n"
            f"o=linphone 1290 2331 IN IP4 {ip}\r\n"
            "s=Talk\r\n"
            f"c=IN IP4 {ip}\r\n"
            "t=0 0\r\n"
            "a=rtcp-xr:rcvr-rtt=all:10000 stat-summary=loss,dup,jitt,TTL voip-metrics\r\n"
            "a=record:off\r\n"
            f"m=audio {port} {profile} {pts}\r\n"
            "a=rtpmap:96 opus/48000/2\r\n"
            "a=fmtp:96 useinbandfec=1\r\n"
            "a=rtpmap:97 speex/16000\r\n"
            "a=fmtp:97 vbr=on\r\n"
            "a=rtpmap:98 speex/8000\r\n"
            "a=rtpmap:101 telephone-event/48000\r\n"
            "a=rtpmap:99 telephone-event/16000\r\n"
            "a=rtpmap:100 telephone-event/8000\r\n"
            "a=rtcp-fb:* trr-int 1000\r\n"
            "a=rtcp-fb:* ccm tmmbr\r\n")


class _Collect(asyncio.DatagramProtocol):
    def __init__(self, sink):
        self.sink = sink

    def datagram_received(self, data, addr):
        self.sink.append((time.monotonic(), data, addr))


class FakePhone:
    """Speaks SIP like Linphone Android: Via with rport, compact-free headers, Contact with params, SDP offering
    opus/speex/PCMU/PCMA/G729/telephone-event. It advertises one RTP port in SDP but actually sends its RTP from
    a different socket (as phones behind odd network stacks do), so JARVIS must learn the address from packets."""

    def __init__(self, ip):
        self.ip = ip
        self.sip, self.rtp = [], []
        self.call_id = uuid.uuid4().hex
        self.tag = uuid.uuid4().hex[:10]
        self.cseq = 20

    async def open(self):
        loop = asyncio.get_running_loop()
        self.sip_tr, _ = await loop.create_datagram_endpoint(lambda: _Collect(self.sip), local_addr=(self.ip, 0))
        self.sip_port = self.sip_tr.get_extra_info("sockname")[1]
        # RTP: a plain socket + reader thread, like a phone's media engine (independent of the asyncio loop)
        self.rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp_sock.bind((self.ip, 0))
        self.rtp_sock.settimeout(0.1)
        self.rtp_port = self.rtp_sock.getsockname()[1]
        self._running = True

        def reader():
            while self._running:
                try:
                    data, addr = self.rtp_sock.recvfrom(4096)
                    self.rtp.append((time.monotonic(), data, addr))
                except socket.timeout:
                    continue
                except OSError:
                    if not self._running:
                        return
        self._reader = threading.Thread(target=reader, daemon=True)
        self._reader.start()
        dead = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        dead.bind((self.ip, 0))
        self.sdp_port = dead.getsockname()[1]   # advertised in SDP but nobody listens there
        dead.close()

    def close(self):
        self.sip_tr.close()
        self._running = False
        self._reader.join(1)
        self.rtp_sock.close()

    def headers(self, method, target, to, cseq=None, branch=None):
        return [f"{method} {target} SIP/2.0",
                f"Via: SIP/2.0/UDP {self.ip}:{self.sip_port};branch={branch or 'z9hG4bK.' + uuid.uuid4().hex[:9]};rport",
                f"From: <sip:linphone@{self.ip}>;tag={self.tag}", f"To: {to}",
                f"CSeq: {cseq or self.cseq} {method}", f"Call-ID: {self.call_id}", "Max-Forwards: 70",
                "Supported: replaces, outbound, gruu, path, record-aware",
                "Allow: INVITE, ACK, CANCEL, OPTIONS, BYE, REFER, NOTIFY, MESSAGE, SUBSCRIBE, INFO, PRACK, UPDATE",
                f"Contact: <sip:linphone@{self.ip}:{self.sip_port};transport=udp>;expires=3600;+org.linphone.specs=\"lime\"",
                "User-Agent: LinphoneAndroid/5.2.5 (Pixel 7) LinphoneSDK/5.3.47"]

    def invite(self, server_ip, server_port, branch=None):
        self.invite_branch = branch or "z9hG4bK." + uuid.uuid4().hex[:9]
        body = linphone_sdp(self.ip, self.sdp_port)
        h = self.headers("INVITE", f"sip:jarvis@{server_ip}:{server_port}", f"sip:jarvis@{server_ip}:{server_port}",
                         branch=self.invite_branch)
        return "\r\n".join(h + ["Content-Type: application/sdp", f"Content-Length: {len(body)}"]) + "\r\n\r\n" + body

    def request(self, method, target, to, cseq=None):
        return "\r\n".join(self.headers(method, target, to, cseq) + ["Content-Length: 0"]) + "\r\n\r\n"

    def send_sip(self, text, port, host=None):
        self.sip_tr.sendto(text.encode(), (host or self.ip, port))

    def responses(self):
        from jarvis.telephony.sip_lan import parse_sip
        return [parse_sip(d) for _, d, _ in self.sip]

    def send_rtp(self, payload, port, host, pt=0, seq=0, ts=0):
        self.rtp_sock.sendto(struct.pack("!BBHII", 0x80, pt, seq & 0xFFFF, ts, 0x1234) + payload, (host, port))


async def _wait(cond, seconds=5.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        await asyncio.sleep(0.02)
    return cond()


def _sip_status_ok(msgs, code):
    return [m for m in msgs if m.is_response and m.code == code]


@pytest.mark.parametrize("where", ["loopback", "lan"])
def test_linphone_style_call_into_manager(settings, where):
    """Full call: INVITE -> 100/180/200 -> ACK, greeting over RTP (latched), caller speech -> STT -> reply,
    DTMF ignored, OPTIONS keepalive, INVITE retransmission, then BYE from the phone."""
    from jarvis.telephony.codec import to_phone
    from jarvis.telephony.sip_lan import local_ip, Offer
    if where == "lan":
        phone_ip = local_ip()
        if phone_ip.startswith("127."):
            pytest.skip("no LAN interface on this machine")
        settings.update({"telephony": {"sip_lan_enabled": True, "sip_bind_host": "0.0.0.0", "sip_port": 0,
                                       "sip_allowed_ips": [f"{phone_ip}/32"]}})
    else:
        phone_ip = "127.0.0.1"
        settings.update({"telephony": {"sip_lan_enabled": True, "sip_bind_host": "127.0.0.1", "sip_port": 0}})

    async def go():
        mgr, events = make_manager(settings, ["Got it. I'll pass that on."], FakeSTT(["please tell him I called"]))
        await mgr.start()
        server = mgr.providers["sip_lan"]
        phone = FakePhone(phone_ip)
        await phone.open()
        try:
            phone.send_sip(phone.invite(phone_ip, server.port), server.port)
            assert await _wait(lambda: _sip_status_ok(phone.responses(), 200)), \
                f"no 200 OK over {where} ({phone_ip}); got {[m.start for m in phone.responses()]}"
            msgs = phone.responses()
            codes = [m.code for m in msgs]
            assert codes.index(100) < codes.index(180) < codes.index(200)
            ringing, ok = _sip_status_ok(msgs, 180)[0], _sip_status_ok(msgs, 200)[0]
            assert ";tag=" in ringing.get("to") and ringing.get("to") == ok.get("to")
            via = ok.get("via")
            assert f"rport={phone.sip_port}" in via and f"received={phone_ip}" in via and phone.invite_branch in via
            answer = Offer(ok.body)
            assert answer.codecs == ["PCMU"] and answer.dtmf_pt == 100 and answer.ip == phone_ip
            assert ok.get("contact") and phone_ip in ok.get("contact")
            # retransmitted INVITE (phone didn't see the 200 yet): same response again, no second call
            phone.send_sip(phone.invite(phone_ip, server.port, branch=phone.invite_branch), server.port)
            assert await _wait(lambda: len(_sip_status_ok(phone.responses(), 200)) >= 2)
            assert len(mgr.calls) == 1
            # ACK to the Contact, as Linphone does
            server_to = ok.get("to")
            phone.send_sip(phone.request("ACK", f"sip:jarvis@{phone_ip}:{server.port}", server_to, cseq=20), server.port)
            assert await _wait(lambda: next(iter(mgr.calls.values())).call.state == CallState.ACTIVE)
            rt = next(iter(mgr.calls.values()))
            assert rt.call.peer_ip == phone_ip and rt.call.owner is False and rt.conversation.tools is None
            assert await _wait(lambda: rt.call.transcript)
            assert "AI assistant" in rt.call.transcript[0]["text"]
            # the phone starts sending RTP from a port that is NOT the one in its SDP; first a DTMF event packet
            phone.send_rtp(b"\x01\x0a\x00\xa0", answer.port, phone_ip, pt=100)
            speech = to_phone(tone(0.8), 16000, "PCMU") + to_phone(np.zeros(16000, np.int16), 16000, "PCMU")
            for i in range(0, len(speech), 160):
                phone.send_rtp(speech[i:i + 160], answer.port, phone_ip, seq=i // 160 + 1, ts=i)
                if i % 1600 == 0:
                    await asyncio.sleep(0.01)
            # greeting + reply arrive as RTP on the socket the phone really sends from (symmetric RTP)
            assert await _wait(lambda: any(t["role"] == "caller" for t in rt.call.transcript), 6), (server.peer_rtp_stats(rt.call), rt.call.transcript, rt.pending.shape, rt.in_utt)
            assert await _wait(lambda: [t["role"] for t in rt.call.transcript][:3] == ["jarvis", "caller", "jarvis"], 6)
            assert rt.call.transcript[1]["text"] == "please tell him I called"
            stats = server.peer_rtp_stats(rt.call)
            assert stats["latched"] and stats["remote"] == (phone_ip, phone.rtp_port) and stats["ignored"] >= 1
            await asyncio.sleep(0.3)
            pkts = [(t, d) for t, d, _ in phone.rtp if len(d) == 172]
            assert pkts, "JARVIS's audio reached the phone over RTP"
            assert all(d[1] & 0x7F == 0 for _, d in pkts), "PCMU payload type"
            voiced = [d for _, d in pkts if d[12:].count(b"\xff") < 150]
            assert voiced, "greeting audio (not just silence) arrived"
            ts = [struct.unpack("!I", d[4:8])[0] for _, d in pkts]
            assert all(b - a == 160 for a, b in zip(ts, ts[1:])), "20 ms frames, contiguous timestamps"
            span = pkts[-1][0] - pkts[0][0]
            assert 0.015 * (len(pkts) - 1) <= span <= 0.03 * (len(pkts) - 1) + 0.3, "paced at ~20 ms"
            # OPTIONS keepalive mid-call
            phone.send_sip(phone.request("OPTIONS", f"sip:jarvis@{phone_ip}:{server.port}", f"<sip:jarvis@{phone_ip}>", 21),
                           server.port)
            assert await _wait(lambda: any(m.code == 200 and "OPTIONS" in m.get("cseq") for m in phone.responses()))
            # the phone hangs up
            phone.send_sip(phone.request("BYE", f"sip:jarvis@{phone_ip}:{server.port}", server_to, 22), server.port)
            assert await _wait(lambda: any(m.code == 200 and "BYE" in m.get("cseq") for m in phone.responses()))
            assert await _wait(lambda: not mgr.calls)
            assert mgr.status()["recent_calls"][-1]["state"] == "ended"
        finally:
            phone.close()
            await mgr.stop()

    asyncio.run(go())


def test_lan_ip_outside_allowlist_is_ignored(settings):
    from jarvis.telephony.sip_lan import local_ip
    phone_ip = local_ip()
    if phone_ip.startswith("127."):
        pytest.skip("no LAN interface on this machine")
    # safe default allowlist: only the Windows Mobile Hotspot subnet, so the hostel/home Wi-Fi address is ignored
    settings.update({"telephony": {"sip_lan_enabled": True, "sip_bind_host": "0.0.0.0", "sip_port": 0,
                                   "sip_allowed_ips": ["192.168.137.0/24"]}})
    if phone_ip.startswith("192.168.137."):
        settings.update({"telephony": {"sip_allowed_ips": ["10.255.255.0/24"]}})

    async def go():
        mgr, _ = make_manager(settings)
        await mgr.start()
        server = mgr.providers["sip_lan"]
        phone = FakePhone(phone_ip)
        await phone.open()
        phone.send_sip(phone.invite(phone_ip, server.port), server.port)
        phone.send_sip(phone.request("OPTIONS", f"sip:jarvis@{phone_ip}:{server.port}", "<sip:jarvis@x>"), server.port)
        await asyncio.sleep(0.6)
        assert phone.sip == [] and not mgr.calls
        assert server.dropped.get(phone_ip, 0) >= 2
        phone.close()
        await mgr.stop()

    asyncio.run(go())


def test_cancel_before_answer_and_480_on_reject(settings):
    settings.update({"telephony": {"sip_lan_enabled": True, "sip_bind_host": "127.0.0.1", "sip_port": 0}})

    async def go():
        mgr, _ = make_manager(settings)
        await mgr.start()
        server = mgr.providers["sip_lan"]
        phone = FakePhone("127.0.0.1")
        await phone.open()
        phone.send_sip(phone.invite("127.0.0.1", server.port), server.port)
        assert await _wait(lambda: _sip_status_ok(phone.responses(), 180), 2)
        # CANCEL uses the INVITE's branch and CSeq number
        h = phone.headers("CANCEL", f"sip:jarvis@127.0.0.1:{server.port}", f"sip:jarvis@127.0.0.1:{server.port}",
                          cseq=20, branch=phone.invite_branch)
        phone.send_sip("\r\n".join(h + ["Content-Length: 0"]) + "\r\n\r\n", server.port)
        assert await _wait(lambda: _sip_status_ok(phone.responses(), 487), 2)
        assert any(m.code == 200 and "CANCEL" in m.get("cseq") for m in phone.responses())
        assert not _sip_status_ok(phone.responses(), 200) or all("CANCEL" in m.get("cseq") for m in _sip_status_ok(phone.responses(), 200))
        assert await _wait(lambda: not mgr.calls, 2)
        # BYE for a call that doesn't exist -> 481
        phone.call_id = uuid.uuid4().hex
        phone.send_sip(phone.request("BYE", f"sip:jarvis@127.0.0.1:{server.port}", "<sip:jarvis@127.0.0.1>;tag=zz"), server.port)
        assert await _wait(lambda: _sip_status_ok(phone.responses(), 481), 2)
        phone.close()
        await mgr.stop()

    asyncio.run(go())


def test_reinvite_hold_and_jarvis_hangs_up(settings):
    settings.update({"telephony": {"sip_lan_enabled": True, "sip_bind_host": "127.0.0.1", "sip_port": 0}})

    async def go():
        mgr, _ = make_manager(settings)
        await mgr.start()
        server = mgr.providers["sip_lan"]
        phone = FakePhone("127.0.0.1")
        await phone.open()
        phone.send_sip(phone.invite("127.0.0.1", server.port), server.port)
        assert await _wait(lambda: _sip_status_ok(phone.responses(), 200))
        to = _sip_status_ok(phone.responses(), 200)[0].get("to")
        phone.send_sip(phone.request("ACK", f"sip:jarvis@127.0.0.1:{server.port}", to, 20), server.port)
        assert await _wait(lambda: mgr.calls and next(iter(mgr.calls.values())).call.state == CallState.ACTIVE)
        call = next(iter(mgr.calls.values())).call
        # re-INVITE putting JARVIS on hold (a=sendonly) -> 200 with a=recvonly; then resume
        for direction, expect in (("sendonly", "recvonly"), ("sendrecv", "sendrecv")):
            phone.cseq += 1
            body = linphone_sdp("127.0.0.1", phone.sdp_port) + f"a={direction}\r\n"
            h = phone.headers("INVITE", f"sip:jarvis@127.0.0.1:{server.port}", to)
            phone.send_sip("\r\n".join(h + ["Content-Type: application/sdp", f"Content-Length: {len(body)}"]) + "\r\n\r\n" + body,
                           server.port)
            assert await _wait(lambda: any(m.code == 200 and m.get("cseq") == f"{phone.cseq} INVITE" for m in phone.responses()))
            resp = [m for m in phone.responses() if m.code == 200 and m.get("cseq") == f"{phone.cseq} INVITE"][0]
            assert f"a={expect}" in resp.body
        assert call.state == CallState.ACTIVE
        # UPDATE without body (session refresh) -> 200
        phone.send_sip(phone.request("UPDATE", f"sip:jarvis@127.0.0.1:{server.port}", to, 40), server.port)
        assert await _wait(lambda: any(m.code == 200 and "UPDATE" in m.get("cseq") for m in phone.responses()))
        # JARVIS hangs up: the phone receives a BYE addressed to its Contact, inside the dialog
        await mgr.hangup(call.id)
        assert await _wait(lambda: any(m.method == "BYE" for m in phone.responses()))
        bye = [m for m in phone.responses() if m.method == "BYE"][0]
        assert bye.uri.startswith(f"sip:linphone@127.0.0.1:{phone.sip_port}")
        assert bye.get("call-id") == phone.call_id and phone.tag in bye.get("to") and ";tag=" in bye.get("from")
        assert mgr.status()["recent_calls"][-1]["state"] == "ended"
        phone.close()
        await mgr.stop()

    asyncio.run(go())


def test_sip_over_tcp_options_and_invite(settings):
    async def go():
        server = SipLanProvider("127.0.0.1", 0, advertise_ip="127.0.0.1")
        await server.start()
        if not server.tcp_server:
            await server.stop()
            pytest.skip("TCP port busy")
        got = []
        server.on_incoming = lambda call: (got.append(call), asyncio.ensure_future(server.answer(call)))
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        phone = FakePhone("127.0.0.1")
        phone.sip_port, phone.sdp_port = writer.get_extra_info("sockname")[1], 7078
        writer.write(phone.invite("127.0.0.1", server.port).replace("SIP/2.0/UDP", "SIP/2.0/TCP").encode())
        data = b""
        end = time.monotonic() + 3
        while b"SIP/2.0 200" not in data and time.monotonic() < end:
            try:
                data += await asyncio.wait_for(reader.read(65536), 0.5)
            except asyncio.TimeoutError:
                pass
        assert b"SIP/2.0 180 Ringing" in data and b"SIP/2.0 200 OK" in data and got
        writer.close()
        await server.stop()

    asyncio.run(go())


def test_owner_mode_gives_tools_only_to_owner_ips(settings):
    settings.update({"telephony": {"sip_lan_enabled": True, "sip_bind_host": "127.0.0.1", "sip_port": 0}})
    toolbox = object()

    async def go():
        events = []
        mgr = TelephonyManager(settings, FakeLLM(), FakeSTT(), FakeTTS(), EnergyVAD, events.append, tools=toolbox)
        await mgr.start()
        server = mgr.providers["sip_lan"]
        for owner_ips, expect_owner in (([], False), (["10.1.2.0/24"], False), (["127.0.0.1"], True)):
            settings.update({"telephony": {"owner_ips": owner_ips}})
            phone = FakePhone("127.0.0.1")
            await phone.open()
            phone.send_sip(phone.invite("127.0.0.1", server.port), server.port)
            assert await _wait(lambda: mgr.calls, 2)
            rt = next(iter(mgr.calls.values()))
            assert rt.call.owner is expect_owner
            assert (rt.conversation.tools is toolbox) is expect_owner
            if expect_owner:
                assert "Use a tool only when" in rt.conversation.messages()[0]["content"]
            await mgr.hangup(rt.call.id)
            assert await _wait(lambda: not mgr.calls, 2)
            phone.close()
        await mgr.stop()

    asyncio.run(go())


def test_phone_setup_payload(settings):
    async def go():
        mgr, _ = make_manager(settings)
        await mgr.start()
        info = mgr.phone_setup()
        assert info["allowed"] == ["127.0.0.0/8 (this PC)", "192.168.137.0/24"]
        for a in info["addresses"]:
            assert a["uri"] == f"sip:jarvis@{a['ip']}:5062" and a["qr_svg"].startswith("<svg")
            if a["ip"].startswith("172.22."):
                assert a["callers_allowed"] is False   # hostel Wi-Fi is not allowlisted by default
        await mgr.stop()

    asyncio.run(go())

    asyncio.run(go())


def test_sip_call_into_jarvis_gets_spoken_greeting(settings):
    """A softphone-like SIP client calls the manager over loopback and receives JARVIS's greeting as G.711 RTP."""
    async def go():
        settings.update({"telephony": {"sip_lan_enabled": True, "sip_bind_host": "127.0.0.1", "sip_port": 0}})
        mgr, events = make_manager(settings, ["Got it. I'll pass that on."], FakeSTT(["please tell him I called"]))
        await mgr.start()
        server = mgr.providers["sip_lan"]
        phone = SipLanProvider("127.0.0.1", 0, advertise_ip="127.0.0.1", user="phone")
        await phone.start()
        heard = []
        phone.on_audio = lambda call, payload: heard.append(payload)
        from jarvis.telephony.base import CallKind, CallSession
        out = CallSession(direction="outbound", remote=f"sip:jarvis@127.0.0.1:{server.port}", provider="sip_lan", kind=CallKind.SIP_LAN)
        await phone.dial(out)
        voiced = lambda: [p for p in heard if p.count(b"\xff") < 150]  # noqa: E731  (silence frames don't count)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if out.state == CallState.ACTIVE and voiced():
                break
        assert out.state == CallState.ACTIVE
        inbound = next(iter(mgr.calls.values())).call
        assert inbound.kind.value == "sip_lan" and "AI assistant" in inbound.transcript[0]["text"]
        await asyncio.sleep(0.3)
        assert sum(len(p) for p in voiced()) >= 800, "greeting audio arrived over RTP"
        await phone.hangup(out)
        await asyncio.sleep(0.2)
        assert mgr.status()["recent_calls"][-1]["state"] == "ended"
        await phone.stop()
        await mgr.stop()

    asyncio.run(go())


@pytest.mark.parametrize("heard,found,rest", [
    ("Jarvis, what time is it?", True, "what time is it?"),
    ("Hey Jarvis open notepad", True, "open notepad"),
    ("Travis, open youtube.", True, "open youtube."),
    ("Jervis what's up", True, "what's up"),
    ("Jarvis's turn, tell me a joke", True, "turn, tell me a joke"),
    ("Okay so I told him about dinner", False, None),
    ("This is my service ticket", False, None),
])
def test_fuzzy_wake_word(heard, found, rest):
    from jarvis.engine import find_wake_word
    ok, text = find_wake_word(heard, "jarvis")
    assert ok == found
    if found:
        assert text == rest


def test_hotkey_combo_parsing():
    from jarvis.hotkey import MOD_ALT, MOD_CONTROL, parse
    assert parse("ctrl+alt+space") == (MOD_CONTROL | MOD_ALT, 0x20)
    assert parse("Ctrl + Alt + J") == (MOD_CONTROL | MOD_ALT, ord("J"))
    with pytest.raises(ValueError):
        parse("space")


def test_listen_once_needs_no_wake_word(settings):
    """Global hotkey: one sentence is accepted without 'Jarvis', even in push-to-talk mode."""
    settings.update({"mode": "push_to_talk", "activation": "wake_word", "silence_ms": 300})

    async def go():
        ScriptedMic.script = [("silence", 0.5), ("say", tone(0.6)), ("silence", 1.5)]
        eng = Engine(settings, EnergyVAD(), FakeSTT(["open the pod bay doors"]), FakeLLM(["I'm afraid I can't."]), FakeTTS(),
                     None, mic_factory=ScriptedMic, speaker_factory=SilentSpeaker)
        await eng.start()
        q = eng.subscribe()
        eng.listen_once()
        await asyncio.sleep(2.8)
        ev = []
        while not q.empty():
            ev.append(q.get_nowait())
        await eng.shutdown()
        return ev

    ev = asyncio.run(go())
    said = [e["text"] for e in ev if e["type"] == "transcript"]
    assert said[0] == "open the pod bay doors" and not any(e["type"] == "ignored" for e in ev)
