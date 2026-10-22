"""Direct-IP SIP endpoint (RFC 3261 subset) + RTP/G.711 for free softphones on your own network.

Scope, honestly: no phone numbers, no carrier, no internet, no authentication, no ICE/STUN/SRTP. A free softphone
(Linphone, Zoiper, MicroSIP) on the same local network can call sip:jarvis@<laptop-ip>:5062 and JARVIS can call a
softphone's LAN address. Only addresses inside the allowlist (CIDR subnets or single IPs, plus loopback) get any
answer at all; everything else is silently dropped.

What real softphones send and how it is handled:
  * INVITE with SDP offering opus/PCMU/PCMA/telephone-event (any order)  -> we pick PCMU, then PCMA (else 488)
  * Via with ;rport                       -> responses carry received=/rport= and go back to the packet's source
  * INVITE retransmissions over UDP       -> the last response is re-sent (no duplicate calls)
  * 100 Trying -> 180 Ringing -> 200 OK   -> 200 OK is retransmitted until the ACK arrives (RFC 3261 17.2.1)
  * re-INVITE / UPDATE (hold, codec, address change)  -> answered with our SDP; remote media address updated
  * OPTIONS keepalives, INFO (DTMF), MESSAGE, NOTIFY, PRACK -> 200 OK;  REGISTER -> 200 OK (no auth, LAN only)
  * CANCEL before answer -> 200 + 487;  BYE from either side; 481 for unknown dialogs
  * symmetric RTP / comedia: the remote RTP address is learnt from the first packet (phones often send from a
    different port, or a different address, than the one in their SDP)
  * telephone-event (DTMF) and comfort-noise payloads are ignored; outgoing RTP is a steady 20 ms stream
    (silence when JARVIS is quiet, so softphones' "no RTP" timers never fire)
  * SIP over UDP and TCP on the same port
"""
from __future__ import annotations

import asyncio
import ipaddress
import random
import re
import socket
import struct
import time
import uuid

from .base import CallKind, CallSession, CallState, TelephonyProvider

PAYLOAD_TYPES = {"PCMU": 0, "PCMA": 8}
CODEC_PREFERENCE = ("PCMU", "PCMA")
FRAME_BYTES = 160  # 20 ms at 8 kHz
SILENCE = {"PCMU": b"\xff", "PCMA": b"\xd5"}
DEFAULT_ALLOWED = ["192.168.137.0/24"]   # Windows Mobile Hotspot clients only (plus loopback, always)
HOTSPOT_PRESETS = {
    "Windows Mobile Hotspot (phone joins the laptop)": "192.168.137.0/24",
    "iPhone Personal Hotspot (laptop joins the iPhone)": "172.20.10.0/28",
    "Android hotspot, classic range (laptop joins the phone)": "192.168.43.0/24",
}
ALLOW = "INVITE, ACK, BYE, CANCEL, OPTIONS, UPDATE, INFO, PRACK, MESSAGE, NOTIFY, REGISTER"
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact", "l": "content-length",
           "c": "content-type", "k": "supported", "s": "subject", "e": "content-encoding"}
T1 = 0.5


# ---------------------------------------------------------------- network helpers
def local_ip() -> str:
    """IP of the interface holding the default route (may be a network phones can't reach!)."""
    return local_ip_for("10.255.255.255")


def local_ip_for(peer: str) -> str:
    """The laptop's own address on the interface that reaches `peer` (no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def lan_addresses() -> list[dict]:
    """Every private IPv4 address on an adapter that is up: [{ip, iface, prefix, network}]."""
    out: list[dict] = []
    try:
        import psutil
        stats = psutil.net_if_stats()
        for iface, addrs in psutil.net_if_addrs().items():
            if iface in stats and not stats[iface].isup:
                continue
            for a in addrs:
                if a.family != socket.AF_INET or not a.address:
                    continue
                ip = ipaddress.ip_address(a.address)
                if ip.is_loopback or ip.is_link_local or not ip.is_private:
                    continue
                net = ipaddress.ip_network(f"{a.address}/{a.netmask or '255.255.255.0'}", strict=False)
                out.append({"ip": a.address, "iface": iface, "prefix": net.prefixlen, "network": str(net)})
    except Exception:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if ipaddress.ip_address(ip).is_private and not ipaddress.ip_address(ip).is_loopback:
                    out.append({"ip": ip, "iface": "?", "prefix": 24,
                                "network": str(ipaddress.ip_network(f"{ip}/24", strict=False))})
        except OSError:
            pass
    seen, uniq = set(), []
    for a in out:
        if a["ip"] not in seen:
            seen.add(a["ip"])
            uniq.append(a)
    return uniq


class Allowlist:
    """Single IPs and CIDR subnets. Loopback is allowed unless loopback=False. Invalid entries are ignored (and reported)."""

    def __init__(self, entries: list[str] | None, loopback: bool = True):
        self.networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = \
            [ipaddress.ip_network("127.0.0.0/8")] if loopback else []
        self.invalid: list[str] = []
        self.entries: list[str] = []
        for e in entries or []:
            e = str(e).strip()
            if not e:
                continue
            try:
                net = ipaddress.ip_network(e, strict=False)
            except ValueError:
                self.invalid.append(e)
                continue
            if not (net.is_private or net.is_loopback):   # never 0.0.0.0/0 or internet addresses
                self.invalid.append(e)
                continue
            self.networks.append(net)
            self.entries.append(e)

    def __contains__(self, ip: str) -> bool:
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in n for n in self.networks)


def _udp_socket(host: str, port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        # Windows: an ICMP "port unreachable" would otherwise break the socket's next receive.
        try:
            s.ioctl(socket.SIO_UDP_CONNRESET, False)  # type: ignore[attr-defined]
        except (OSError, ValueError):
            pass
    s.bind((host, port))
    s.setblocking(False)
    return s


# ---------------------------------------------------------------- SIP message helpers
class SipMessage:
    def __init__(self, start: str, headers: list[tuple[str, str]], body: str):
        self.start, self.headers, self.body = start, headers, body
        self.is_response = start.startswith("SIP/2.0")
        parts = start.split(" ", 2)
        self.code = int(parts[1]) if self.is_response and len(parts) > 1 and parts[1].isdigit() else 0
        self.method = "" if self.is_response else parts[0].upper()
        self.uri = "" if self.is_response or len(parts) < 2 else parts[1]

    def get(self, name: str, default: str = "") -> str:
        for k, v in self.headers:
            if k == name:
                return v
        return default

    def all(self, name: str) -> list[str]:
        out = []
        for k, v in self.headers:
            if k == name:
                out.extend(_split_header(v) if name == "via" else [v])
        return out

    @property
    def cseq(self) -> tuple[int, str]:
        m = re.match(r"\s*(\d+)\s+(\S+)", self.get("cseq"))
        return (int(m.group(1)), m.group(2).upper()) if m else (0, "")


def _split_header(v: str) -> list[str]:
    return [p.strip() for p in re.split(r",(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", v) if p.strip()]


def parse_sip(data: bytes) -> SipMessage:
    text = data.decode("utf-8", "replace")
    head, _, body = text.partition("\r\n\r\n")
    if "\r\n\r\n" not in text and "\n\n" in text:
        head, _, body = text.partition("\n\n")
    lines = re.split(r"\r?\n", head)
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if line[:1] in (" ", "\t") and headers:      # folded header line
            headers[-1] = (headers[-1][0], headers[-1][1] + " " + line.strip())
        elif ":" in line:
            k, v = line.split(":", 1)
            k = k.strip().lower()
            headers.append((COMPACT.get(k, k), v.strip()))
    msg = SipMessage(lines[0].strip(), headers, body)
    length = msg.get("content-length")
    if length.isdigit():
        msg.body = body.encode("utf-8", "replace")[:int(length)].decode("utf-8", "replace")
    return msg


def _tag(header: str) -> str:
    m = re.search(r";\s*tag=([^;>\s,]+)", header)
    return m.group(1) if m else ""


def _uri(header: str) -> str:
    m = re.search(r"<([^>]+)>", header)
    if m:
        return m.group(1)
    return header.split(";")[0].strip()


def _caller_name(from_header: str) -> str:
    uri = _uri(from_header)
    name = re.match(r'\s*"?([^"<]*?)"?\s*<', from_header)
    label = re.sub(r"^sips?:", "", uri)
    return f"{name.group(1).strip()} ({label})" if name and name.group(1).strip() else label


# ---------------------------------------------------------------- SDP
def sdp(ip: str, port: int, codecs: list[str], session: tuple[int, int] | None = None,
        direction: str = "sendrecv", dtmf_pt: int | None = None) -> str:
    sid, ver = session or (random.randint(1, 1 << 30), 1)
    pts = [str(PAYLOAD_TYPES[c]) for c in codecs] + ([str(dtmf_pt)] if dtmf_pt is not None else [])
    rtpmap = "".join(f"a=rtpmap:{PAYLOAD_TYPES[c]} {c}/8000\r\n" for c in codecs)
    if dtmf_pt is not None:
        rtpmap += f"a=rtpmap:{dtmf_pt} telephone-event/8000\r\na=fmtp:{dtmf_pt} 0-15\r\n"
    return (f"v=0\r\no=jarvis {sid} {ver} IN IP4 {ip}\r\ns=JARVIS\r\nc=IN IP4 {ip}\r\nt=0 0\r\n"
            f"m=audio {port} RTP/AVP {' '.join(pts)}\r\n{rtpmap}a=ptime:20\r\na={direction}\r\n")


class Offer:
    """The parts of a remote SDP we care about."""

    def __init__(self, body: str):
        self.ip, self.port, self.profile = "", 0, ""
        self.payloads: list[int] = []
        self.names: dict[int, str] = {}
        self.direction = "sendrecv"
        self.dtmf_pt: int | None = None
        session_ip, media_ip, in_audio = "", "", False
        for line in re.split(r"\r?\n", body or ""):
            line = line.strip()
            if line.startswith("m="):
                if self.port:          # already have the first audio stream; ignore later m= lines
                    in_audio = False
                    continue
                m = re.match(r"m=audio (\d+)(?:/\d+)? (\S+) ([\d ]+)", line)
                in_audio = bool(m)
                if m:
                    self.port, self.profile = int(m.group(1)), m.group(2).upper()
                    self.payloads = [int(x) for x in m.group(3).split()]
            elif line.startswith("c="):
                m = re.match(r"c=IN IP4 (\S+)", line)
                if m:
                    if in_audio:
                        media_ip = m.group(1)
                    elif not self.port:
                        session_ip = m.group(1)
            elif line.startswith("a=") and (in_audio or not self.port):
                m = re.match(r"a=rtpmap:(\d+) ([^/\s]+)/(\d+)", line)
                if m:
                    self.names[int(m.group(1))] = m.group(2)
                    if m.group(2).lower() == "telephone-event" and m.group(3) == "8000":
                        self.dtmf_pt = int(m.group(1))
                elif line[2:] in ("sendrecv", "sendonly", "recvonly", "inactive"):
                    self.direction = line[2:]
        self.ip = (media_ip or session_ip).split("/")[0]
        if self.dtmf_pt not in self.payloads:
            self.dtmf_pt = None

    @property
    def codecs(self) -> list[str]:
        out = []
        for pt in self.payloads:
            name = self.names.get(pt, {0: "PCMU", 8: "PCMA"}.get(pt, "")).upper()
            if name in PAYLOAD_TYPES and PAYLOAD_TYPES[name] == pt:
                out.append(name)
        return out

    @property
    def encrypted_only(self) -> bool:
        return "SAVP" in self.profile

    @property
    def held(self) -> bool:
        return self.ip in ("0.0.0.0", "") or self.direction in ("sendonly", "inactive")


def parse_sdp(body: str) -> tuple[str, int, list[str]]:
    o = Offer(body)
    return o.ip, o.port, o.codecs


def choose_codec(offered: list[str]) -> str | None:
    return next((c for c in CODEC_PREFERENCE if c in offered), None)


_ANSWER_DIRECTION = {"sendrecv": "sendrecv", "sendonly": "recvonly", "recvonly": "sendonly", "inactive": "inactive"}


# ---------------------------------------------------------------- transports
class _Udp:
    """UDP socket with its own receive loop.

    Deliberately not asyncio's datagram transport: on Windows (proactor loop) a single ConnectionResetError from
    recvfrom (an ICMP "port unreachable", e.g. RTCP or RTP aimed at a port the phone closed) makes that transport
    stop reading for good, and the call would go deaf. Here errors are skipped and reading continues."""

    def __init__(self, sock: socket.socket, handler):
        self.sock, self.handler = sock, handler
        self.transport = self            # keeps the old `proto.transport.sendto` shape
        self.closed = False
        self.errors = 0
        self.task = asyncio.ensure_future(self._run())

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self.closed:
            try:
                data, addr = await loop.sock_recvfrom(self.sock, 65535)
            except asyncio.CancelledError:
                return
            except OSError:
                if self.closed:
                    return
                self.errors += 1
                await asyncio.sleep(0.001 if self.errors % 100 else 0.05)
                continue
            try:
                self.handler(data, addr[:2])
            except Exception:
                pass

    def sendto(self, data: bytes, addr) -> None:
        if self.closed:
            return
        try:
            self.sock.sendto(data, addr)
        except OSError:
            pass  # full buffer or unreachable peer: drop this datagram, like any UDP stack

    def get_extra_info(self, name: str):
        return self.sock.getsockname() if name == "sockname" else None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.task.cancel()
            self.sock.close()


class _Rtp:
    """One RTP stream (plus an RTCP sink on port+1). Outgoing audio is a steady 20 ms packet stream."""

    def __init__(self, provider: "SipLanProvider", call: CallSession):
        self.provider, self.call = provider, call
        self.transport: _Udp | None = None
        self.rtcp: _Udp | None = None
        self.port = 0
        self.remote: tuple[str, int] | None = None     # where we send (SDP first, then latched)
        self.hosts: set[str] = set()                    # addresses we accept media from
        self.latched = False
        self.sending = True     # False while the phone has us on hold
        self.started = False    # media flows only once the call is answered
        self.seq, self.ts, self.ssrc = random.randint(0, 65535), random.randint(0, 1 << 31), random.randint(1, 1 << 31)
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()
        self.sender: asyncio.Task | None = None
        self.packets_in = self.packets_out = 0
        self.ignored_in = 0

    async def open(self, host: str, port_range: tuple[int, int] | None = None) -> None:
        candidates: list[int] = [0]
        if port_range and port_range[0] > 0 and port_range[1] > port_range[0]:
            lo, hi = port_range[0] + port_range[0] % 2, port_range[1]
            candidates = list(range(lo, hi, 2))
            random.shuffle(candidates)
            candidates.append(0)
        for port in candidates:
            try:
                rtp_sock = _udp_socket(host, port)
            except OSError:
                continue
            real = rtp_sock.getsockname()[1]
            try:
                rtcp_sock = _udp_socket(host, real + 1)
            except OSError:
                rtcp_sock = None
                if port:
                    rtp_sock.close()
                    continue
            self.transport = _Udp(rtp_sock, self._on_packet)
            if rtcp_sock:
                self.rtcp = _Udp(rtcp_sock, lambda d, a: None)   # RTCP is read and ignored
            self.port = real
            break
        else:
            raise OSError("no free RTP port")
        self.sender = asyncio.create_task(self._pace())

    def set_remote(self, ip: str, port: int, signalling_ip: str) -> None:
        self.hosts = {signalling_ip} | ({ip} if ip and ip != "0.0.0.0" else set())
        if not self.latched or (self.remote and self.remote[0] not in self.hosts):
            self.latched = False
            if ip and ip != "0.0.0.0" and port:
                self.remote = (ip, port)
            elif port:
                self.remote = (signalling_ip, port)

    def _on_packet(self, data: bytes, addr) -> None:
        if len(data) < 12 or (data[0] >> 6) != 2 or addr[0] not in self.hosts:
            return
        pt = data[1] & 0x7F
        if 72 <= pt <= 79:      # RTCP multiplexed on the RTP port (rtcp-mux)
            return
        if not self.latched or self.remote != addr:
            self.remote, self.latched = (addr[0], addr[1]), True     # symmetric RTP / comedia
        if pt != PAYLOAD_TYPES.get(self.call.codec):
            self.ignored_in += 1   # telephone-event (DTMF), comfort noise, opus...: not ours
            return
        offset = 12 + 4 * (data[0] & 0x0F)
        if data[0] & 0x10 and len(data) >= offset + 4:
            offset += 4 + 4 * struct.unpack("!H", data[offset + 2:offset + 4])[0]
        end = len(data) - (data[-1] if data[0] & 0x20 else 0)
        payload = data[offset:end]
        if payload:
            self.packets_in += 1
            self.provider.on_audio(self.call, payload)

    async def _pace(self) -> None:
        next_t = time.perf_counter()
        first = True
        while True:
            try:
                frame = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                frame = SILENCE.get(self.call.codec, b"\xff") * FRAME_BYTES
            if self.remote and self.transport and self.sending and self.started:
                pt = PAYLOAD_TYPES.get(self.call.codec, 0)
                header = struct.pack("!BBHII", 0x80, pt | (0x80 if first else 0), self.seq & 0xFFFF,
                                     self.ts & 0xFFFFFFFF, self.ssrc)
                self.transport.sendto(header + frame, self.remote)
                self.packets_out += 1
                first = False
            self.seq += 1
            self.ts += FRAME_BYTES
            next_t += 0.02
            delay = next_t - time.perf_counter()
            if delay < -0.2:            # we fell far behind (laptop busy): resync instead of bursting
                next_t, delay = time.perf_counter(), 0.0
            await asyncio.sleep(max(0.0, delay))

    def send(self, payload: bytes) -> None:
        silence = SILENCE.get(self.call.codec, b"\xff")
        for i in range(0, len(payload), FRAME_BYTES):
            frame = payload[i:i + FRAME_BYTES]
            self.queue.put_nowait(frame + silence * (FRAME_BYTES - len(frame)))

    def flush(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()

    def close(self) -> None:
        if self.sender:
            self.sender.cancel()
        for t in (self.transport, self.rtcp):
            if t:
                t.close()


class SipLanProvider(TelephonyProvider):
    name = "sip_lan"
    kind = CallKind.SIP_LAN
    reaches_pstn = False
    zero_cost_verified = True   # direct IP on your own network involves no carrier or account

    def __init__(self, bind_host: str = "0.0.0.0", port: int = 5062, allowed_ips=None,
                 advertise_ip: str | None = None, user: str = "jarvis", rtp_ports: tuple[int, int] | None = None,
                 tcp: bool = True):
        """`allowed_ips`: list of IPs/CIDR subnets, or a callable returning one (read on every packet, so settings
        changes apply live). None -> DEFAULT_ALLOWED. Loopback is always allowed."""
        super().__init__()
        self.bind_host, self.port, self.user = bind_host, port, user
        self._allowed_src = allowed_ips if allowed_ips is not None else list(DEFAULT_ALLOWED)
        self._allow_cache: tuple[tuple, Allowlist] | None = None
        self.extra_allowed: set[str] = set()      # hosts JARVIS dialled itself
        self.allow_loopback = True                # this PC can always call itself (tests, local softphone)
        self.advertise_ip = advertise_ip
        self.ip = advertise_ip or (local_ip() if bind_host == "0.0.0.0" else bind_host)
        self.rtp_ports = rtp_ports
        self.use_tcp = tcp
        self.proto: _Udp | None = None
        self.tcp_server: asyncio.base_events.Server | None = None
        self.dialogs: dict[str, dict] = {}       # Call-ID -> dialog state
        self.by_call: dict[str, str] = {}        # CallSession.id -> Call-ID
        self.registrations: dict[str, dict] = {}
        self.dropped: dict[str, int] = {}        # unknown source IP -> packets ignored
        self.last_error = ""

    # ---------- allowlist ----------
    @property
    def allowlist(self) -> Allowlist:
        src = self._allowed_src() if callable(self._allowed_src) else self._allowed_src
        entries = list(src or []) or list(DEFAULT_ALLOWED)
        key = (tuple(entries), self.allow_loopback)
        if not self._allow_cache or self._allow_cache[0] != key:
            self._allow_cache = (key, Allowlist(entries, loopback=self.allow_loopback))
        return self._allow_cache[1]

    @property
    def allowed(self):   # backwards compatible: tests may assign a set of IPs / subnets
        return self.allowlist

    @allowed.setter
    def allowed(self, value) -> None:
        self._allowed_src = list(value)
        self._allow_cache = None

    def is_allowed(self, ip: str) -> bool:
        return ip in self.allowlist or ip in self.extra_allowed

    # ---------- lifecycle ----------
    async def start(self) -> None:
        sock = _udp_socket(self.bind_host, self.port)
        self.proto = _Udp(sock, self._on_udp)
        self.port = sock.getsockname()[1]
        if self.use_tcp:
            try:
                self.tcp_server = await asyncio.start_server(self._on_tcp, self.bind_host, self.port)
            except OSError as exc:
                self.tcp_server, self.last_error = None, f"SIP over TCP unavailable: {exc}"

    async def stop(self) -> None:
        for d in list(self.dialogs.values()):
            await self.hangup(d["call"])
        if self.proto and self.proto.transport:
            self.proto.transport.close()
        self.proto = None
        if self.tcp_server:
            self.tcp_server.close()
            self.tcp_server = None

    def uris(self) -> list[str]:
        if self.advertise_ip or self.bind_host not in ("0.0.0.0", ""):
            return [f"sip:{self.user}@{self.ip}:{self.port}"]
        return [f"sip:{self.user}@{a['ip']}:{self.port}" for a in lan_addresses()] or \
            [f"sip:{self.user}@{self.ip}:{self.port}"]

    def status(self) -> dict:
        al = self.allowlist
        return super().status() | {
            "listening": bool(self.proto), "tcp": bool(self.tcp_server), "port": self.port,
            "uri": f"sip:{self.user}@{self.ip}:{self.port}", "uris": self.uris(),
            "allowed_ips": ["127.0.0.0/8 (this PC)"] + al.entries, "invalid_allowed": al.invalid,
            "registrations": [{"aor": k, **v} for k, v in self.registrations.items()],
            "ignored_sources": dict(list(self.dropped.items())[-10:]), "error": self.last_error}

    def peer_rtp_stats(self, call: CallSession) -> dict:
        d = self.dialogs.get(self.by_call.get(call.id, ""))
        if not d:
            return {}
        r = d["rtp"]
        return {"in": r.packets_in, "out": r.packets_out, "ignored": r.ignored_in, "remote": r.remote, "latched": r.latched}

    # ---------- transport ----------
    def _on_udp(self, data: bytes, addr) -> None:
        if not self.is_allowed(addr[0]):
            self.dropped[addr[0]] = self.dropped.get(addr[0], 0) + 1
            return  # silently drop: unknown hosts on the network can't reach JARVIS
        if not data.strip():
            return  # CRLF keepalive
        self._handle(data, {"kind": "udp", "addr": addr})

    async def _on_tcp(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        addr = writer.get_extra_info("peername")[:2]
        if not self.is_allowed(addr[0]):
            self.dropped[addr[0]] = self.dropped.get(addr[0], 0) + 1
            writer.close()
            return
        target = {"kind": "tcp", "addr": addr, "writer": writer}
        buf = b""
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                buf += chunk
                while True:
                    stripped = buf.lstrip(b"\r\n")
                    if len(stripped) != len(buf):
                        if buf.startswith(b"\r\n\r\n"):
                            writer.write(b"\r\n")   # RFC 5626 keepalive pong
                        buf = stripped
                    end = buf.find(b"\r\n\r\n")
                    if end < 0:
                        break
                    m = re.search(rb"(?im)^(?:content-length|l)\s*:\s*(\d+)", buf[:end])
                    total = end + 4 + (int(m.group(1)) if m else 0)
                    if len(buf) < total:
                        break
                    self._handle(buf[:total], target)
                    buf = buf[total:]
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    def _send(self, text: str, target) -> None:
        if isinstance(target, tuple):
            target = {"kind": "udp", "addr": target}
        data = text.encode()
        if target["kind"] == "tcp":
            w = target["writer"]
            if not w.is_closing():
                w.write(data)
                return
        if self.proto and self.proto.transport:
            self.proto.transport.sendto(data, target["addr"])

    def _handle(self, data: bytes, target: dict) -> None:
        try:
            msg = parse_sip(data)
        except Exception:
            return
        if not msg.is_response and not msg.method:
            return
        asyncio.ensure_future(self._dispatch_safe(msg, target))

    async def _dispatch_safe(self, msg: SipMessage, target: dict) -> None:
        try:
            await (self._on_response(msg, target) if msg.is_response else self._on_request(msg, target))
        except Exception as exc:  # never let one malformed packet kill the listener
            self.last_error = f"{type(exc).__name__}: {exc}"

    def _local_ip(self, peer: str) -> str:
        if self.advertise_ip:
            return self.advertise_ip
        if self.bind_host not in ("0.0.0.0", ""):
            return self.bind_host
        return local_ip_for(peer)

    # ---------- responses ----------
    def _build_response(self, code: int, reason: str, req: SipMessage, target: dict, body: str = "",
                        to_tag: str = "", local_ip: str = "", extra: list[str] | None = None) -> str:
        vias = req.all("via")
        if vias:
            top = vias[0]
            src_ip, src_port = target["addr"]
            if re.search(r";\s*rport(?=;|$|\s)", top):
                top = re.sub(r";\s*rport(?=;|$|\s)", f";rport={src_port}", top)
            if "received=" not in top:
                top += f";received={src_ip}"
            vias[0] = top
        to = req.get("to")
        if to_tag and code > 100 and not _tag(to):
            to += f";tag={to_tag}"
        transport = "tcp" if target["kind"] == "tcp" else "udp"
        lines = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in vias] + [
            f"From: {req.get('from')}", f"To: {to}", f"Call-ID: {req.get('call-id')}", f"CSeq: {req.get('cseq')}"]
        if code > 100 and req.method in ("INVITE", "UPDATE", "OPTIONS", "REGISTER", "PRACK") or code in (180, 183):
            ip = local_ip or self._local_ip(target["addr"][0])
            if req.method != "REGISTER":
                lines.append(f"Contact: <sip:{self.user}@{ip}:{self.port};transport={transport}>")
        lines += ["User-Agent: JARVIS-Zero", f"Allow: {ALLOW}", "Supported: replaces"] + (extra or [])
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body.encode())}")
        return "\r\n".join(lines) + "\r\n\r\n" + body

    def _respond(self, code: int, reason: str, req: SipMessage, target: dict, **kw) -> str:
        text = self._build_response(code, reason, req, target, **kw)
        self._send(text, target)
        return text

    # ---------- requests from the phone ----------
    async def _on_request(self, req: SipMessage, target: dict) -> None:
        call_id = req.get("call-id")
        method = req.method
        d = self.dialogs.get(call_id)
        if method == "INVITE":
            if d is None:
                await self._new_invite(req, target)
            elif d["role"] == "uas" and req.cseq[0] == d["invite_cseq"] and not d.get("acked"):
                if d.get("last_response"):
                    self._send(d["last_response"], target)   # retransmitted INVITE
            else:
                self._reinvite(d, req, target)
        elif method == "ACK":
            if d and d["role"] == "uas" and not d.get("acked") and d.get("answered"):
                d["acked"] = True
                if d.get("retx"):
                    d["retx"].cancel()
                if req.body.strip():
                    self._apply_offer(d, Offer(req.body), target)
                call = d["call"]
                if call.state == CallState.RINGING:
                    call.state = CallState.ACTIVE
                    call.answered_at = call.answered_at or time.time()
                    self.on_state(call)
        elif method == "CANCEL":
            if d and d["role"] == "uas" and not d.get("answered"):
                self._respond(200, "OK", req, target)
                self._respond(487, "Request Terminated", d["req"], d["target"], to_tag=d["tag"])
                self._end(call_id, CallState.ENDED, "Caller hung up before JARVIS answered")
            elif d:
                self._respond(200, "OK", req, target)
            else:
                self._respond(481, "Call/Transaction Does Not Exist", req, target)
        elif method == "BYE":
            if d:
                self._respond(200, "OK", req, target)
                self._end(call_id, CallState.ENDED)
            else:
                self._respond(481, "Call/Transaction Does Not Exist", req, target)
        elif method == "UPDATE":
            if d:
                self._reinvite(d, req, target)
            else:
                self._respond(481, "Call/Transaction Does Not Exist", req, target)
        elif method == "OPTIONS":
            self._respond(200, "OK", req, target, extra=["Accept: application/sdp"])
        elif method == "REGISTER":
            contact = req.get("contact")
            m = re.search(r"expires=(\d+)", contact) or re.match(r"(\d+)", req.get("expires", "3600"))
            expires = min(int(m.group(1)) if m else 3600, 3600)
            aor = _uri(req.get("to"))
            if expires == 0:
                self.registrations.pop(aor, None)
            else:
                self.registrations[aor] = {"contact": _uri(contact), "ip": target["addr"][0], "until": time.time() + expires}
            self._respond(200, "OK", req, target, extra=([f"Contact: {contact.split(';expires=')[0]};expires={expires}"]
                                                          if contact and expires else []) + [f"Expires: {expires}"])
        elif method in ("INFO", "MESSAGE", "NOTIFY", "PRACK"):
            self._respond(200, "OK", req, target)   # DTMF over INFO and chat messages are ignored
        else:
            self._respond(405, "Method Not Allowed", req, target)

    async def _new_invite(self, req: SipMessage, target: dict) -> None:
        offer = Offer(req.body)
        tag = uuid.uuid4().hex[:8]
        if not req.body.strip():
            self._respond(488, "Not Acceptable Here", req, target, to_tag=tag,
                          extra=["Warning: 399 jarvis \"Send an SDP offer in the INVITE\""])
            return
        codec = choose_codec(offer.codecs)
        if offer.encrypted_only or not codec:
            why = "Turn off media encryption (SRTP/ZRTP) in the softphone" if offer.encrypted_only \
                else "Enable the PCMU or PCMA (G.711) codec in the softphone"
            self._respond(488, "Not Acceptable Here", req, target, to_tag=tag, extra=[f'Warning: 399 jarvis "{why}"'])
            return
        call_id = req.get("call-id")
        local_ip = self._local_ip(target["addr"][0])
        call = CallSession(direction="inbound", remote=_caller_name(req.get("from")), provider=self.name,
                           kind=self.kind, state=CallState.RINGING, codec=codec)
        call.peer_ip = target["addr"][0]
        rtp = _Rtp(self, call)
        await rtp.open(self.bind_host or "0.0.0.0", self.rtp_ports)
        rtp.set_remote(offer.ip, offer.port, target["addr"][0])
        rtp.sending = offer.direction in ("sendrecv", "recvonly")
        d = {"call": call, "rtp": rtp, "target": target, "req": req, "tag": tag, "role": "uas",
             "invite_cseq": req.cseq[0], "local_ip": local_ip, "cseq": 0, "call_id": call_id,
             "local_uri": req.get("to"), "remote_uri": req.get("from"),
             "remote_target": _uri(req.get("contact")) or f"sip:{target['addr'][0]}:{target['addr'][1]}",
             "sdp_session": (random.randint(1, 1 << 30), 1), "dtmf_pt": offer.dtmf_pt,
             "direction": _ANSWER_DIRECTION.get(offer.direction, "sendrecv"), "transport": target["kind"].upper()}
        self.dialogs[call_id] = d
        self.by_call[call.id] = call_id
        self._respond(100, "Trying", req, target)
        d["last_response"] = self._respond(180, "Ringing", req, target, to_tag=tag, local_ip=local_ip)
        self.on_incoming(call)

    def _apply_offer(self, d: dict, offer: Offer, target: dict) -> None:
        rtp = d["rtp"]
        if offer.port and offer.ip != "0.0.0.0":
            rtp.set_remote(offer.ip, offer.port, target["addr"][0])
        rtp.sending = offer.direction in ("sendrecv", "recvonly") and offer.ip != "0.0.0.0"
        d["direction"] = "sendonly" if offer.ip == "0.0.0.0" else _ANSWER_DIRECTION.get(offer.direction, "sendrecv")
        if offer.dtmf_pt is not None:
            d["dtmf_pt"] = offer.dtmf_pt

    def _our_sdp(self, d: dict, bump: bool = False) -> str:
        sid, ver = d["sdp_session"]
        if bump:
            d["sdp_session"] = (sid, ver + 1)
        return sdp(d["local_ip"], d["rtp"].port, [d["call"].codec], d["sdp_session"], d.get("direction", "sendrecv"),
                   d.get("dtmf_pt"))

    def _reinvite(self, d: dict, req: SipMessage, target: dict) -> None:
        if req.body.strip():
            offer = Offer(req.body)
            if d["call"].codec not in offer.codecs:
                new = choose_codec(offer.codecs)
                if not new:
                    self._respond(488, "Not Acceptable Here", req, target)
                    return
                d["call"].codec = new
            self._apply_offer(d, offer, target)
        if req.method == "UPDATE" and not req.body.strip():
            self._respond(200, "OK", req, target, local_ip=d["local_ip"])
            return
        self._respond(200, "OK", req, target, body=self._our_sdp(d, bump=True), local_ip=d["local_ip"])
        if req.get("contact"):
            d["remote_target"] = _uri(req.get("contact"))

    async def _retransmit_ok(self, call_id: str) -> None:
        """RFC 3261 13.3.1.4: resend the 2xx with T1 doubling until the ACK arrives (max 64*T1)."""
        interval, waited = T1, 0.0
        while waited < 64 * T1:
            await asyncio.sleep(interval)
            waited += interval
            d = self.dialogs.get(call_id)
            if not d or d.get("acked"):
                return
            if d["target"]["kind"] == "udp":
                self._send(d["last_response"], d["target"])
            interval = min(interval * 2, 4.0)
        d = self.dialogs.get(call_id)
        if d and not d.get("acked"):
            self._send(self._request("BYE", d), d["target"])
            self._end(call_id, CallState.FAILED, "The phone never acknowledged the answer (ACK missing)")

    def _end(self, call_id: str, state: CallState, error: str = "") -> None:
        d = self.dialogs.pop(call_id, None)
        if not d:
            return
        self.by_call.pop(d["call"].id, None)
        for key in ("retx", "invite_retx"):
            if d.get(key):
                d[key].cancel()
        d["rtp"].close()
        call = d["call"]
        call.state = state
        if error and state == CallState.FAILED:
            call.error = error
        call.ended_at = time.time()
        self.on_state(call)

    # ---------- our own requests ----------
    def _request(self, method: str, d: dict, body: str = "", branch: str | None = None, cseq: int | None = None) -> str:
        if cseq is None:
            d["cseq"] += 1
            cseq = d["cseq"]
        ip = d.get("local_ip") or self.ip
        transport = d.get("transport", "UDP")
        if d["role"] == "uas":
            frm, to = d["local_uri"], d["remote_uri"]
            if not _tag(frm):
                frm += f";tag={d['tag']}"
        else:
            frm, to = f"<sip:{self.user}@{ip}:{self.port}>;tag={d['tag']}", d.get("to") or f"<{d['target_uri']}>"
        uri = d.get("remote_target") or d.get("target_uri")
        lines = [f"{method} {uri} SIP/2.0",
                 f"Via: SIP/2.0/{transport} {ip}:{self.port};branch={branch or 'z9hG4bK' + uuid.uuid4().hex[:12]};rport",
                 "Max-Forwards: 70", f"From: {frm}", f"To: {to}", f"Call-ID: {d['call_id']}",
                 f"CSeq: {cseq} {method}", f"Contact: <sip:{self.user}@{ip}:{self.port};transport={transport.lower()}>",
                 "User-Agent: JARVIS-Zero"]
        if method == "INVITE":
            lines.append(f"Allow: {ALLOW}")
        if body:
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body.encode())}")
        return "\r\n".join(lines) + "\r\n\r\n" + body

    async def _on_response(self, resp: SipMessage, target: dict) -> None:
        call_id = resp.get("call-id")
        d = self.dialogs.get(call_id)
        if not d or d["role"] != "uac":
            return
        call = d["call"]
        num, method = resp.cseq
        if method != "INVITE" or num != d["invite_cseq"]:
            return  # responses to our BYE/CANCEL need no action
        if d.get("invite_retx"):
            d["invite_retx"].cancel()
            d["invite_retx"] = None
        if resp.code in (180, 183) and call.state == CallState.DIALING:
            call.state = CallState.RINGING
            self.on_state(call)
        elif 200 <= resp.code < 300:
            d["to"] = resp.get("to")
            if resp.get("contact"):
                d["remote_target"] = _uri(resp.get("contact"))
            offer = Offer(resp.body)
            if offer.port:
                d["rtp"].set_remote(offer.ip, offer.port, target["addr"][0])
                call.codec = choose_codec(offer.codecs) or call.codec
            d["rtp"].started = True
            self._send(self._request("ACK", d, cseq=d["invite_cseq"]), target)
            if call.state not in (CallState.ACTIVE, CallState.HANDOFF):
                call.state = CallState.ACTIVE
                call.answered_at = time.time()
                self.on_state(call)
        elif resp.code >= 300:
            # ACK for a non-2xx final response reuses the INVITE's branch (RFC 3261 17.1.1.3)
            d["to"] = resp.get("to")
            self._send(self._request("ACK", d, branch=d["invite_branch"], cseq=d["invite_cseq"]), target)
            warning = re.search(r'"([^"]+)"', resp.get("warning"))
            reason = resp.start.split(" ", 2)[2] if len(resp.start.split(" ", 2)) > 2 else ""
            self._end(call_id, CallState.FAILED,
                      f"Remote answered {resp.code} {reason}".strip() + (f" ({warning.group(1)})" if warning else ""))

    async def _retransmit_invite(self, call_id: str, text: str) -> None:
        interval, waited = T1, 0.0
        while waited < 16.0:
            await asyncio.sleep(interval)
            waited += interval
            d = self.dialogs.get(call_id)
            if not d or d["call"].state != CallState.DIALING:
                return
            self._send(text, d["target"])
            interval = min(interval * 2, 4.0)
        d = self.dialogs.get(call_id)
        if d and d["call"].state == CallState.DIALING:
            d["invite_retx"] = None
            self._end(call_id, CallState.FAILED, "No answer from that address (is the phone on the same network?)")

    # ---------- provider API ----------
    async def dial(self, call: CallSession) -> None:
        m = re.match(r"^sip:(?:([^@]+)@)?([^:;>]+)(?::(\d+))?", call.remote)
        if not m:
            raise ValueError("bad SIP URI")
        host, port = m.group(2), int(m.group(3) or 5060)
        rtp = _Rtp(self, call)
        await rtp.open(self.bind_host or "0.0.0.0", self.rtp_ports)
        rtp.hosts = {host}
        call_id = uuid.uuid4().hex
        branch = "z9hG4bK" + uuid.uuid4().hex[:12]
        d = {"call": call, "rtp": rtp, "target": {"kind": "udp", "addr": (host, port)}, "tag": uuid.uuid4().hex[:8],
             "role": "uac", "cseq": 1, "invite_cseq": 1, "invite_branch": branch, "call_id": call_id,
             "target_uri": call.remote.split(">")[0], "local_ip": self._local_ip(host), "transport": "UDP",
             "sdp_session": (random.randint(1, 1 << 30), 1), "direction": "sendrecv", "dtmf_pt": None}
        self.dialogs[call_id] = d
        self.by_call[call.id] = call_id
        self.extra_allowed.add(host)
        call.peer_ip = host
        call.state = CallState.DIALING
        self.on_state(call)
        invite = self._request("INVITE", d, sdp(d["local_ip"], rtp.port, list(CODEC_PREFERENCE), dtmf_pt=101),
                               branch=branch, cseq=1)
        d["invite_text"] = invite
        self._send(invite, d["target"])
        d["invite_retx"] = asyncio.ensure_future(self._retransmit_invite(call_id, invite))

    async def answer(self, call: CallSession) -> None:
        d = self.dialogs.get(self.by_call.get(call.id, ""))
        if d and d["role"] == "uas" and not d.get("answered"):
            d["answered"] = True
            d["rtp"].started = True
            d["last_response"] = self._respond(200, "OK", d["req"], d["target"], body=self._our_sdp(d),
                                               to_tag=d["tag"], local_ip=d["local_ip"])
            d["retx"] = asyncio.ensure_future(self._retransmit_ok(d["call_id"]))

    async def hangup(self, call: CallSession) -> None:
        call_id = self.by_call.get(call.id, "")
        d = self.dialogs.get(call_id)
        if not d:
            return
        if d["role"] == "uas":
            if d.get("answered"):
                self._send(self._request("BYE", d), d["target"])
            else:
                self._respond(480, "Temporarily Unavailable", d["req"], d["target"], to_tag=d["tag"])
        elif call.state in (CallState.ACTIVE, CallState.HANDOFF):
            self._send(self._request("BYE", d), d["target"])
        else:
            # CANCEL matches the INVITE: same branch, same CSeq number (RFC 3261 9.1)
            text = d["invite_text"].split("\r\n\r\n")[0].split("\r\n")
            cancel = [ln for ln in text if not ln.lower().startswith(("content-type", "content-length", "allow", "contact"))]
            cancel[0] = cancel[0].replace("INVITE ", "CANCEL ", 1)
            cancel = [re.sub(r"^CSeq: (\d+) INVITE", r"CSeq: \1 CANCEL", ln) for ln in cancel]
            self._send("\r\n".join(cancel + ["Content-Length: 0"]) + "\r\n\r\n", d["target"])
        self._end(call_id, CallState.ENDED)

    async def send_audio(self, call: CallSession, payload: bytes) -> None:
        d = self.dialogs.get(self.by_call.get(call.id, ""))
        if d:
            d["rtp"].send(payload)

    def flush_audio(self, call: CallSession) -> None:
        d = self.dialogs.get(self.by_call.get(call.id, ""))
        if d:
            d["rtp"].flush()
