"""Open an RTSP or RTSPS URL and read real video frames from it.

This is a client, not a replay: it sends DESCRIBE, SETUP and PLAY, then counts
the H.264 frames the server actually sends.
"""

from __future__ import annotations

import base64
import hashlib
import re
import socket
import ssl
import struct
import time
from urllib.parse import urlsplit, urlunsplit

_HIGH_PROFILES = {100, 110, 122, 244, 44, 83, 86, 118, 128, 138, 139, 134, 135}


class ProbeError(Exception):
    pass


def safe_url(url: str) -> str:
    parts = urlsplit(url)
    if not parts.username:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, ""))


def probe(url: str, timeout: float = 10, bind_ip: str = "") -> dict:
    """Play `url` until one complete video frame arrives, or report why it did not."""
    started = time.monotonic()
    result = {"ok": False, "url": safe_url(url), "codec": "", "width": 0, "height": 0, "frames": 0, "packets": 0, "slices": 0, "error": ""}
    try:
        _play(url, timeout, bind_ip, result)
    except (ProbeError, TimeoutError, OSError, ssl.SSLError) as exc:
        result["error"] = str(exc) or exc.__class__.__name__
    result["seconds"] = round(time.monotonic() - started, 1)
    return result


def h264_size(sps_nal: bytes) -> tuple[int, int]:
    """Width and height from an H.264 SPS NAL, including its header byte."""
    bits = _Bits(_rbsp(sps_nal[1:]))
    profile = bits.u(8)
    bits.u(8)
    bits.u(8)
    bits.ue()
    chroma = 1
    if profile in _HIGH_PROFILES:
        chroma = bits.ue()
        if chroma == 3:
            bits.u(1)
        bits.ue()
        bits.ue()
        bits.u(1)
        if bits.u(1):
            raise ValueError("scaling matrices are not decoded")
    bits.ue()
    order = bits.ue()
    if order == 0:
        bits.ue()
    elif order == 1:
        bits.u(1)
        bits.se()
        bits.se()
        for _ in range(bits.ue()):
            bits.se()
    bits.ue()
    bits.u(1)
    width = (bits.ue() + 1) * 16
    height_units = bits.ue() + 1
    frames_only = bits.u(1)
    height = (2 - frames_only) * height_units * 16
    if not frames_only:
        bits.u(1)
    bits.u(1)
    if bits.u(1):
        crop = 2 if chroma <= 2 else 1
        height_crop = crop * (2 - frames_only)
        width -= (bits.ue() + bits.ue()) * crop
        height -= (bits.ue() + bits.ue()) * height_crop
    return width, height


def _play(url: str, timeout: float, bind_ip: str, result: dict) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"rtsp", "rtsps"}:
        raise ProbeError(f"{parts.scheme or 'this'} is not an RTSP stream")
    host = parts.hostname
    if not host:
        raise ProbeError("the stream address has no host")
    port = parts.port or (322 if parts.scheme == "rtsps" else 554)
    deadline = time.monotonic() + timeout
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.settimeout(min(5, timeout))
        if bind_ip:
            sock.bind((bind_ip, 0))
        sock.connect((host, port))
        if parts.scheme == "rtsps":
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            sock = context.wrap_socket(sock, server_hostname=host)
        target = urlunsplit((parts.scheme, f"{host}:{port}", parts.path or "/", parts.query, ""))
        cseq = 1
        status, headers, body = _request(sock, "DESCRIBE", target, cseq, {"Accept": "application/sdp"}, parts, deadline)
        if status == 401:
            cseq += 1
            challenge = headers.get("www-authenticate", "")
            status, headers, body = _request(
                sock, "DESCRIBE", target, cseq, {"Accept": "application/sdp", "Authorization": _authorization(challenge, "DESCRIBE", target, parts)}, parts, deadline
            )
        if status != 200:
            raise ProbeError(f"the stream refused DESCRIBE ({status})")
        text = body.decode("utf-8", "replace")
        codec, control = _video_track(text)
        result["codec"] = codec
        found = re.search(r"sprop-parameter-sets=([A-Za-z0-9+/=]+)", text)
        if found:
            try:
                result["width"], result["height"] = h264_size(base64.b64decode(found.group(1)))
            except (ValueError, IndexError):
                pass
        if not control:
            raise ProbeError("the stream has no video track")
        track = _control_url(headers.get("content-base", target), control)
        cseq += 1
        status, setup, _ = _request(
            sock, "SETUP", track, cseq, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"}, parts, deadline
        )
        if status != 200:
            raise ProbeError(f"the stream refused SETUP ({status})")
        session = setup.get("session", "").split(";")[0].strip()
        cseq += 1
        status, _, _ = _request(sock, "PLAY", target, cseq, {"Session": session, "Range": "npt=0.000-"}, parts, deadline)
        if status != 200:
            raise ProbeError(f"the stream refused PLAY ({status})")
        _read_frames(sock, deadline, result)
        if not result["frames"]:
            raise ProbeError("the stream sent no video frames")
        result["ok"] = True
    finally:
        try:
            sock.close()
        except OSError:
            pass


def _request(sock, method: str, url: str, cseq: int, headers: dict, parts, deadline) -> tuple[int, dict, bytes]:
    if parts.username and "Authorization" not in headers:
        token = base64.b64encode(f"{parts.username}:{parts.password or ''}".encode()).decode()
        headers = {**headers, "Authorization": f"Basic {token}"}
    lines = [f"{method} {url} RTSP/1.0", f"CSeq: {cseq}", "User-Agent: StreamManager"]
    lines += [f"{name}: {value}" for name, value in headers.items() if value]
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
    return _response(sock, deadline)


def _response(sock, deadline) -> tuple[int, dict, bytes]:
    data = b""
    while b"\r\n\r\n" not in data:
        data += _chunk(sock, 1, deadline)
        if len(data) > 65536:
            raise ProbeError("the stream sent an invalid response")
    head, rest = data.split(b"\r\n\r\n", 1)
    text = head.decode("utf-8", "replace")
    status = int(text.split(" ", 2)[1])
    headers = {}
    for line in text.split("\r\n")[1:]:
        if ":" in line:
            name, value = line.split(":", 1)
            headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length") or 0)
    while len(rest) < length:
        rest += _chunk(sock, length - len(rest), deadline)
    return status, headers, rest[:length]


def _read_frames(sock, deadline, result: dict) -> None:
    frame_slices = 0
    timestamp = None
    while time.monotonic() < deadline and result["frames"] < 2:
        marker = _chunk(sock, 1, deadline)
        if marker != b"$":
            continue
        channel = _chunk(sock, 1, deadline)[0]
        length = struct.unpack("!H", _chunk(sock, 2, deadline))[0]
        packet = _chunk(sock, length, deadline)
        if channel != 0 or len(packet) < 12:
            continue
        result["packets"] += 1
        stamp = struct.unpack("!I", packet[4:8])[0]
        if timestamp is None:
            timestamp = stamp
        elif stamp != timestamp:
            result["frames"] += 1
            result["slices"] = max(result["slices"], frame_slices)
            frame_slices = 0
            timestamp = stamp
        offset = 12 + (packet[0] & 0x0F) * 4
        if packet[0] & 0x10:
            if len(packet) < offset + 4:
                continue
            offset += 4 + struct.unpack("!H", packet[offset + 2 : offset + 4])[0] * 4
        for nal in _nals(packet[offset:]):
            kind = nal[0] & 0x1F
            if kind in {1, 5}:
                frame_slices += 1
            elif kind == 7 and not result["width"]:
                try:
                    result["width"], result["height"] = h264_size(nal)
                except (ValueError, IndexError):
                    pass
        if packet[1] & 0x80:
            result["frames"] += 1
            result["slices"] = max(result["slices"], frame_slices)
            frame_slices = 0
            timestamp = None


def _nals(payload: bytes) -> list[bytes]:
    if not payload:
        return []
    kind = payload[0] & 0x1F
    if kind == 24:
        found = []
        pos = 1
        while pos + 2 <= len(payload):
            size = struct.unpack("!H", payload[pos : pos + 2])[0]
            found.append(payload[pos + 2 : pos + 2 + size])
            pos += 2 + size
        return found
    if kind == 28 and len(payload) > 2:
        start = payload[1] & 0x80
        if not start:
            return []
        header = bytes([(payload[0] & 0xE0) | (payload[1] & 0x1F)])
        return [header + payload[2:]]
    return [payload]


def _video_track(sdp: str) -> tuple[str, str]:
    codec = ""
    control = ""
    video = False
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            if video and control:
                break
            video = line.startswith("m=video")
            control = ""
        elif video and line.startswith("a=rtpmap:") and not codec:
            codec = line.split(" ", 1)[-1].split("/")[0]
        elif video and line.startswith("a=control:"):
            control = line.split(":", 1)[1].strip()
    return codec, control


def _control_url(base: str, control: str) -> str:
    if control.startswith("rtsp"):
        return control
    return base.rstrip("/") + "/" + control.lstrip("/")


def _authorization(challenge: str, method: str, url: str, parts) -> str:
    if not challenge.lower().startswith("digest"):
        token = base64.b64encode(f"{parts.username}:{parts.password or ''}".encode()).decode()
        return f"Basic {token}"
    fields = {}
    for item in challenge[len("Digest") :].split(","):
        if "=" in item:
            key, value = item.split("=", 1)
            fields[key.strip()] = value.strip().strip('"')
    username = parts.username or ""
    password = parts.password or ""
    realm = fields.get("realm", "")
    nonce = fields.get("nonce", "")
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{url}".encode()).hexdigest()
    qop = fields.get("qop", "").split(",")[0].strip()
    if qop:
        nc = "00000001"
        cnonce = hashlib.md5(nonce.encode()).hexdigest()[:16]
        response = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()
        return (
            f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{url}", '
            f'response="{response}", qop={qop}, nc={nc}, cnonce="{cnonce}"'
        )
    response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    return f'Digest username="{username}", realm="{realm}", nonce="{nonce}", uri="{url}", response="{response}"'


def _chunk(sock, size: int, deadline) -> bytes:
    data = b""
    while len(data) < size:
        if time.monotonic() > deadline:
            raise TimeoutError("timed out waiting for the stream")
        sock.settimeout(max(0.2, deadline - time.monotonic()))
        piece = sock.recv(size - len(data))
        if not piece:
            raise ProbeError("the stream closed the connection")
        data += piece
    return data


def _rbsp(data: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for byte in data:
        if zeros >= 2 and byte == 3:
            zeros = 0
            continue
        out.append(byte)
        zeros = zeros + 1 if byte == 0 else 0
    return bytes(out)


class _Bits:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.pos = 0

    def u(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.pos // 8] >> (7 - self.pos % 8)) & 1)
            self.pos += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
        return (1 << zeros) - 1 + (self.u(zeros) if zeros else 0)

    def se(self) -> int:
        value = self.ue()
        return (value + 1) // 2 if value % 2 else -(value // 2)
