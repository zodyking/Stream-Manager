"""WS-Discovery messages and the multicast listener that answers ONVIF probes."""

from __future__ import annotations

import errno
import itertools
import logging
import re
import select
import socket
import threading
import time
import uuid
from collections import deque
from urllib.parse import quote
from xml.sax.saxutils import escape

log = logging.getLogger("onvif_proxy.discovery")

MULTICAST = ("239.255.255.250", 3702)


def scope_list(name: str) -> list[str]:
    return [
        "onvif://www.onvif.org/Profile/Streaming",
        "onvif://www.onvif.org/Profile/S",
        "onvif://www.onvif.org/type/video_encoder",
        "onvif://www.onvif.org/type/NetworkVideoTransmitter",
        "onvif://www.onvif.org/hardware/VirtualCamera",
        "onvif://www.onvif.org/name/" + quote(name, safe=""),
        "onvif://www.onvif.org/location/virtual",
    ]


def probe_relevant(payload: str) -> bool:
    if "Probe" not in payload or "ProbeMatches" in payload:
        return False
    types = re.search(r"<(?:\w+:)?Types[^>]*>(.*?)</(?:\w+:)?Types>", payload, re.S)
    if types is None:
        return True
    text = types.group(1).strip()
    if not text:
        return True
    return "NetworkVideoTransmitter" in text or re.search(r"(?:^|[\s:])Device(?:\s|$)", text) is not None


def scopes_match(payload: str, scopes: list[str]) -> bool:
    match = re.search(r"<(?:\w+:)?Scopes[^>]*>(.*?)</(?:\w+:)?Scopes>", payload, re.S)
    if match is None:
        return True
    requested = [item for item in match.group(1).split() if item.strip()]
    if not requested:
        return True
    return all(any(scope.startswith(item) or item.startswith(scope) for scope in scopes) for item in requested)


def message_id(payload: str) -> str:
    match = re.search(r"<(?:\w+:)?MessageID[^>]*>([^<]+)", payload)
    if match:
        return match.group(1).strip()
    return "uuid:" + str(uuid.uuid4())


_NAMESPACES = (
    'xmlns:soap="http://www.w3.org/2003/05/soap-envelope" '
    'xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
    'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
    'xmlns:dn="http://www.onvif.org/ver10/network/wsdl" '
    'xmlns:tds="http://www.onvif.org/ver10/device/wsdl"'
)
_TYPES = "dn:NetworkVideoTransmitter tds:Device"
_INSTANCE = str(int(time.time()))


def _endpoint(endpoint_uuid: str, scopes: list[str], xaddr: str) -> str:
    return (
        f"<wsa:EndpointReference><wsa:Address>urn:uuid:{escape(endpoint_uuid)}</wsa:Address></wsa:EndpointReference>"
        f"<d:Types>{_TYPES}</d:Types>"
        f"<d:Scopes>{escape(' '.join(scopes))}</d:Scopes>"
        f"<d:XAddrs>{escape(xaddr)}</d:XAddrs>"
        "<d:MetadataVersion>1</d:MetadataVersion>"
    )


def build_probe_match(relates_to: str, endpoint_uuid: str, scopes: list[str], xaddr: str, number: int = 1) -> bytes:
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope {_NAMESPACES}>
  <soap:Header>
    <wsa:MessageID>uuid:{uuid.uuid4()}</wsa:MessageID>
    <wsa:RelatesTo>{escape(relates_to)}</wsa:RelatesTo>
    <wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>
    <d:AppSequence InstanceId="{_INSTANCE}" MessageNumber="{number}"/>
  </soap:Header>
  <soap:Body>
    <d:ProbeMatches><d:ProbeMatch>{_endpoint(endpoint_uuid, scopes, xaddr)}</d:ProbeMatch></d:ProbeMatches>
  </soap:Body>
</soap:Envelope>"""
    return body.encode("utf-8")


def build_announcement(kind: str, endpoint_uuid: str, scopes: list[str], xaddr: str, number: int = 1) -> bytes:
    """Hello when a camera appears and Bye when it leaves."""
    if kind == "Hello":
        inner = _endpoint(endpoint_uuid, scopes, xaddr)
    else:
        inner = f"<wsa:EndpointReference><wsa:Address>urn:uuid:{escape(endpoint_uuid)}</wsa:Address></wsa:EndpointReference>"
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope {_NAMESPACES}>
  <soap:Header>
    <wsa:MessageID>uuid:{uuid.uuid4()}</wsa:MessageID>
    <wsa:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</wsa:To>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/{kind}</wsa:Action>
    <d:AppSequence InstanceId="{_INSTANCE}" MessageNumber="{number}"/>
  </soap:Header>
  <soap:Body><d:{kind}>{inner}</d:{kind}></soap:Body>
</soap:Envelope>"""
    return body.encode("utf-8")


def _discoverable(ad: dict) -> bool:
    ip = str(ad.get("ip") or "")
    return ad.get("discovery_mode") != "NonDiscoverable" and bool(ip) and not ip.startswith("127.") and ip != "0.0.0.0"


class DiscoveryService:
    """One wildcard socket hears every probe; each camera answers from its own address.

    A socket bound to a unicast address never sees multicast on Linux, so the
    listener binds 0.0.0.0:3702 and joins the group once per camera address.
    """

    def __init__(self) -> None:
        self.status: dict = {"ok": True, "bindings": [], "note": "no cameras"}
        self._ads: list[dict] = []
        self._targets: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None
        self._senders: dict[str, socket.socket] = {}
        self._seen: deque[str] = deque(maxlen=64)
        self._numbers = itertools.count(1)

    def update(self, advertisements: list[dict]) -> None:
        running = self._thread is not None or not any(_discoverable(ad) for ad in advertisements)
        if advertisements == self._ads and running:
            return
        self._ads = list(advertisements)
        self.restart()

    def restart(self) -> None:
        targets = [ad for ad in self._ads if _discoverable(ad)]
        kept = {ad["endpoint_uuid"] for ad in targets}
        self._announce("Bye", [ad for ad in self._targets if ad["endpoint_uuid"] not in kept])
        self.stop(announce=False)
        self._stop = threading.Event()
        self._targets = targets
        bindings: list[dict] = []
        loopback = sum(1 for ad in self._ads if str(ad.get("ip", "")).startswith("127."))
        if loopback:
            bindings.append({"ip": "127.0.0.1", "cameras": loopback, "error": "loopback is not announced with WS-Discovery"})
        if not self._targets:
            self.status = {"ok": True, "bindings": bindings, "note": "no discoverable cameras"}
            return
        try:
            self._listener = self._open_listener()
        except OSError as exc:
            log.warning("WS-Discovery cannot listen on UDP 3702: %s", exc)
            for ip in sorted({ad["ip"] for ad in self._targets}):
                bindings.append({"ip": ip, "cameras": self._count(ip), "error": f"UDP 3702: {exc}"})
            self.status = {"ok": False, "bindings": bindings, "note": "discovery listener failed"}
            return
        for ip in sorted({ad["ip"] for ad in self._targets}):
            error = self._join(ip)
            if error is None:
                try:
                    self._senders[ip] = self._open_sender(ip)
                except OSError as exc:
                    error = str(exc)
            if error:
                log.warning("WS-Discovery on %s: %s", ip, error)
            bindings.append({"ip": ip, "cameras": self._count(ip), "error": error})
        self.status = {
            "ok": not any(item["error"] for item in bindings if item["ip"] != "127.0.0.1"),
            "bindings": bindings,
            "note": "",
        }
        self._thread = threading.Thread(target=self._loop, name="ws-discovery", daemon=True)
        self._thread.start()
        self._announce("Hello")

    def stop(self, announce: bool = True) -> None:
        if announce:
            self._announce("Bye")
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        for sock in [self._listener, *self._senders.values()]:
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass
        self._listener = None
        self._senders = {}
        self._thread = None

    def _count(self, ip: str) -> int:
        return sum(1 for ad in self._targets if ad["ip"] == ip)

    def _open_listener(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", MULTICAST[1]))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        return sock

    def _join(self, ip: str) -> str | None:
        assert self._listener is not None
        request = socket.inet_aton(MULTICAST[0]) + socket.inet_aton(ip)
        try:
            self._listener.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, request)
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                return None
            return f"join multicast: {exc}"
        return None

    def _open_sender(self, ip: str) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.bind((ip, 0))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        except OSError:
            sock.close()
            raise
        return sock

    def _announce(self, kind: str, ads: list[dict] | None = None) -> None:
        for ad in self._targets if ads is None else ads:
            sock = self._senders.get(ad["ip"])
            if sock is None:
                continue
            packet = build_announcement(kind, ad["endpoint_uuid"], ad["scopes"], ad["xaddr"], next(self._numbers))
            try:
                sock.sendto(packet, MULTICAST)
            except OSError as exc:
                log.debug("%s from %s failed: %s", kind, ad["ip"], exc)

    def _loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                readable, _, _ = select.select([listener], [], [], 0.4)
            except (OSError, ValueError):
                return
            if not readable:
                continue
            try:
                data, addr = listener.recvfrom(65535)
            except OSError:
                continue
            self._reply(data, addr)

    def _reply(self, data: bytes, addr: tuple) -> None:
        text = data.decode("utf-8", "replace")
        if not probe_relevant(text):
            return
        relates = message_id(text)
        if relates in self._seen:
            return
        self._seen.append(relates)
        for ad in self._targets:
            if not scopes_match(text, ad["scopes"]):
                continue
            sock = self._senders.get(ad["ip"]) or self._listener
            if sock is None:
                continue
            packet = build_probe_match(relates, ad["endpoint_uuid"], ad["scopes"], ad["xaddr"], next(self._numbers))
            try:
                sock.sendto(packet, addr)
            except OSError as exc:
                log.debug("probe reply from %s failed: %s", ad["ip"], exc)
