"""WS-Discovery ProbeMatch builders and a best-effort multicast listener."""

from __future__ import annotations

import logging
import re
import select
import socket
import threading
import uuid
from urllib.parse import quote
from xml.sax.saxutils import escape

log = logging.getLogger("onvif_proxy.discovery")

MULTICAST = ("239.255.255.250", 3702)


def scope_list(name: str) -> list[str]:
    return [
        "onvif://www.onvif.org/Profile/Streaming",
        "onvif://www.onvif.org/type/video_encoder",
        "onvif://www.onvif.org/type/NetworkVideoTransmitter",
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
    return "NetworkVideoTransmitter" in text


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


def build_probe_match(relates_to: str, endpoint_uuid: str, scopes: list[str], xaddr: str) -> bytes:
    safe_scopes = escape(" ".join(scopes))
    safe_xaddr = escape(xaddr)
    safe_relates = escape(relates_to)
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope" xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <soap:Header>
    <wsa:MessageID>uuid:{uuid.uuid4()}</wsa:MessageID>
    <wsa:RelatesTo>{safe_relates}</wsa:RelatesTo>
    <wsa:To>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</wsa:To>
    <wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action>
    <d:AppSequence InstanceId="1" MessageNumber="1"/>
  </soap:Header>
  <soap:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <wsa:EndpointReference><wsa:Address>urn:uuid:{escape(endpoint_uuid)}</wsa:Address></wsa:EndpointReference>
        <d:Types>dn:NetworkVideoTransmitter</d:Types>
        <d:Scopes>{safe_scopes}</d:Scopes>
        <d:XAddrs>{safe_xaddr}</d:XAddrs>
        <d:MetadataVersion>1</d:MetadataVersion>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </soap:Body>
</soap:Envelope>"""
    return body.encode("utf-8")


class DiscoveryService:
    def __init__(self) -> None:
        self.status: dict = {"ok": True, "bindings": [], "note": "no cameras"}
        self._ads: list[dict] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._sockets: list[socket.socket] = []
        self._by_sock: dict[int, list[dict]] = {}

    def update(self, advertisements: list[dict]) -> None:
        if advertisements == self._ads and (self._thread or not any(ad.get("ip") and not str(ad.get("ip")).startswith("127.") for ad in advertisements)):
            return
        self._ads = list(advertisements)
        self.restart()

    def restart(self) -> None:
        self.stop()
        self._stop = threading.Event()
        bindings, sockets, by_sock = self._bind_all()
        self._sockets = sockets
        self._by_sock = by_sock
        failures = [item for item in bindings if item.get("error") and not str(item.get("ip", "")).startswith("127.")]
        self.status = {
            "ok": not failures,
            "bindings": bindings,
            "note": "" if bindings else "no discoverable cameras",
        }
        if sockets:
            self._thread = threading.Thread(target=self._loop, name="ws-discovery", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        for sock in self._sockets:
            try:
                sock.close()
            except OSError:
                pass
        self._sockets = []
        self._by_sock = {}
        self._thread = None

    def _bind_all(self) -> tuple[list[dict], list[socket.socket], dict[int, list[dict]]]:
        groups: dict[str, list[dict]] = {}
        for ad in self._ads:
            if ad.get("discovery_mode") == "NonDiscoverable":
                continue
            ip = ad.get("ip") or ""
            if not ip or ip.startswith("127."):
                continue
            groups.setdefault(ip, []).append(ad)
        bindings: list[dict] = []
        sockets: list[socket.socket] = []
        by_sock: dict[int, list[dict]] = {}
        for ip, ads in groups.items():
            try:
                sock = self._open(ip)
            except OSError as exc:
                log.warning("discovery bind %s failed: %s", ip, exc)
                bindings.append({"ip": ip, "cameras": len(ads), "error": str(exc)})
                continue
            by_sock[id(sock)] = ads
            sockets.append(sock)
            bindings.append({"ip": ip, "cameras": len(ads), "error": None})
        if any(ad.get("ip", "").startswith("127.") for ad in self._ads):
            bindings.append(
                {
                    "ip": "127.0.0.1",
                    "cameras": sum(1 for ad in self._ads if str(ad.get("ip", "")).startswith("127.")),
                    "error": "loopback is not announced with WS-Discovery",
                }
            )
        return bindings, sockets, by_sock

    def _open(self, ip: str) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((ip, 3702))
            group = socket.inet_aton("239.255.255.250")
            iface = socket.inet_aton(ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, group + iface)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, iface)
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        return sock

    def _loop(self) -> None:
        while not self._stop.is_set():
            sockets = list(self._sockets)
            if not sockets:
                return
            try:
                readable, _, _ = select.select(sockets, [], [], 0.4)
            except (OSError, ValueError):
                return
            for sock in readable:
                try:
                    data, addr = sock.recvfrom(65535)
                except OSError:
                    continue
                self._reply(sock, data, addr)

    def _reply(self, sock: socket.socket, data: bytes, addr: tuple) -> None:
        text = data.decode("utf-8", "replace")
        if not probe_relevant(text):
            return
        relates = message_id(text)
        for ad in self._by_sock.get(id(sock), []):
            scopes = ad["scopes"]
            if not scopes_match(text, scopes):
                continue
            packet = build_probe_match(relates, ad["endpoint_uuid"], scopes, ad["xaddr"])
            try:
                sock.sendto(packet, addr)
            except OSError as exc:
                log.debug("probe reply failed: %s", exc)
