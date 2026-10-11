"""Give each camera its own address, and on wired networks its own MAC.

UniFi Protect tells cameras apart by the MAC address that answers ARP for the
camera IP, so a plain secondary address merges every camera into one device.
Wired interfaces get a macvlan per camera. Wi-Fi access points drop frames from
unknown MACs, so wireless interfaces fall back to a secondary address.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import tempfile
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path

log = logging.getLogger("onvif_proxy.aliases")

LINK_PREFIX = "smv"
_RULE_PRIORITY = "1200"
_NFT_TABLE = "stream_manager"


def link_name(camera_id: str) -> str:
    return (LINK_PREFIX + camera_id)[:15]


def virtual_mac(camera_id: str) -> str:
    digest = hashlib.sha1(("stream-manager:" + camera_id).encode("utf-8")).digest()
    return ":".join(f"{byte:02x}" for byte in (0x02, *digest[:5]))


def route_table(ip: str) -> int:
    return 40000 + (int(IPv4Address(ip)) & 0xFFFF)


def is_wireless(interface: str) -> bool:
    base = Path("/sys/class/net") / interface
    return (base / "wireless").exists() or (base / "phy80211").exists()


def hardware_address(interface: str) -> str:
    try:
        return (Path("/sys/class/net") / interface / "address").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def default_gateway(interface: str) -> str:
    try:
        lines = Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return ""
    for line in lines:
        parts = line.split()
        if len(parts) >= 3 and parts[0] == interface and parts[1] == "00000000" and parts[2] != "00000000":
            return socket.inet_ntoa(struct.pack("<L", int(parts[2], 16)))
    return ""


def _tool(name: str) -> str:
    for folder in ("/sbin", "/usr/sbin", "/bin", "/usr/bin"):
        candidate = os.path.join(folder, name)
        if os.path.isfile(candidate):
            return candidate
    return shutil.which(name) or ""


def _default_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=5)


def _write_sysctl(path: str, value: str) -> bool:
    try:
        Path(path).write_text(value, encoding="ascii")
        return True
    except OSError:
        return False


class AddressBook:
    """Addresses owned by cameras, persisted so they come back after a restart."""

    def __init__(self, path: Path, run=None, sysctl=None, gateway=None) -> None:
        self.path = path
        self._managed = run is None
        self._run = run or _default_run
        self._sysctl = sysctl or _write_sysctl
        self._gateway = gateway or default_gateway
        self._entries: dict[str, dict] = {}
        self.arp_mode = ""
        self._load()

    def entries(self) -> dict[str, dict]:
        return dict(self._entries)

    def restore(self) -> None:
        for ip, entry in list(self._entries.items()):
            try:
                self._apply(ip, entry)
            except ValueError as exc:
                log.warning("could not restore %s: %s", ip, exc)
        self._isolate_arp()

    def teardown(self) -> None:
        """Drop every address from the host but remember them for the next start."""
        for ip, entry in list(self._entries.items()):
            self._remove(ip, entry)
        self._nft_rules({})

    def release(self, ip: str) -> None:
        entry = self._entries.pop(ip, None)
        if entry:
            self._remove(ip, entry)
            self._save()
            self._isolate_arp()

    def ensure(self, interface: str, ip: str, prefix: int, camera_id: str, *, link: str = "", mac: str = "") -> None:
        entry = {"interface": interface, "prefix": prefix, "camera_id": camera_id, "link": link, "mac": mac}
        current = self._entries.get(ip)
        if current and current != entry:
            self._remove(ip, current)
        self._apply(ip, entry)
        self._entries[ip] = entry
        self._save()
        self._isolate_arp()

    def owner(self, ip: str) -> str:
        return str((self._entries.get(ip) or {}).get("camera_id") or "")

    def allocate(
        self,
        interface: str,
        primary_ip: str,
        prefix: int,
        camera_id: str,
        *,
        probe,
        present: set[str] | None = None,
        link: str = "",
        mac: str = "",
    ) -> str:
        owned = set(present or ())
        owned.add(primary_ip)
        owned.update(ip for ip, entry in self._entries.items() if entry.get("camera_id") != camera_id)
        for candidate in _candidates(primary_ip, prefix):
            text = str(candidate)
            if text in owned:
                continue
            if probe(text):
                continue
            self.ensure(interface, text, prefix, camera_id, link=link, mac=mac)
            return text
        raise ValueError(f"no free address on {interface}")

    def _ip(self) -> str:
        binary = "ip" if not self._managed else _tool("ip")
        if not binary:
            raise ValueError("this host cannot add an address on the broadcast network")
        return binary

    def _call(self, args: list[str], *, allow: tuple[str, ...] = ()) -> str:
        if "exist" in allow:
            allow = (*allow, "already assigned")
        completed = self._run(args)
        error = (completed.stderr or "").strip()
        if completed.returncode and not any(word in error.lower() for word in allow):
            raise ValueError(error or f"{' '.join(args[1:3])} failed")
        return completed.stdout or ""

    def _apply(self, ip: str, entry: dict) -> None:
        interface = str(entry.get("interface") or "")
        prefix = int(entry.get("prefix") or 24)
        link = str(entry.get("link") or "")
        if not interface or not ip:
            raise ValueError("this host cannot add an address on the broadcast network")
        ip_bin = self._ip()
        if not link:
            self._call([ip_bin, "addr", "add", f"{ip}/{prefix}", "dev", interface], allow=("exist",))
            return
        mac = str(entry.get("mac") or "")
        self._call(
            [ip_bin, "link", "add", link, "link", interface, "address", mac, "type", "macvlan", "mode", "bridge"],
            allow=("exist",),
        )
        self._call([ip_bin, "link", "set", link, "up"])
        self._call([ip_bin, "addr", "add", f"{ip}/{prefix}", "dev", link, "noprefixroute"], allow=("exist",))
        table = str(route_table(ip))
        network = str(IPv4Network(f"{ip}/{prefix}", strict=False))
        self._call([ip_bin, "route", "replace", network, "dev", link, "src", ip, "table", table])
        gateway = self._gateway(interface)
        if gateway:
            self._call([ip_bin, "route", "replace", "default", "via", gateway, "dev", link, "table", table], allow=("unreachable",))
        rules = self._call([ip_bin, "rule", "show"], allow=("",))
        if f"from {ip} lookup {table}" not in rules:
            self._call([ip_bin, "rule", "add", "from", ip, "lookup", table, "priority", _RULE_PRIORITY])

    def _remove(self, ip: str, entry: dict) -> None:
        interface = str(entry.get("interface") or "")
        link = str(entry.get("link") or "")
        try:
            ip_bin = self._ip()
        except ValueError:
            return
        if link:
            self._run([ip_bin, "rule", "del", "from", ip, "lookup", str(route_table(ip))])
            self._run([ip_bin, "link", "del", link])
        elif interface:
            self._run([ip_bin, "addr", "del", f"{ip}/{int(entry.get('prefix') or 24)}", "dev", interface])

    def _isolate_arp(self) -> None:
        """Only the macvlan may answer ARP for a camera IP, or Protect sees the host MAC."""
        links = {ip: entry for ip, entry in self._entries.items() if entry.get("link")}
        if not links:
            self.arp_mode = ""
            self._nft_rules({})
            return
        written = True
        for entry in links.values():
            for name in {str(entry["link"]), str(entry.get("interface") or "")}:
                if not name:
                    continue
                written &= self._sysctl(f"/proc/sys/net/ipv4/conf/{name}/arp_ignore", "1")
                written &= self._sysctl(f"/proc/sys/net/ipv4/conf/{name}/arp_announce", "2")
            self._sysctl(f"/proc/sys/net/ipv4/conf/{entry['link']}/rp_filter", "2")
        if written:
            self.arp_mode = "sysctl"
            return
        self.arp_mode = "nftables" if self._nft_rules(links) else "unavailable"
        if self.arp_mode == "unavailable":
            log.warning("cannot isolate ARP for camera addresses; UniFi Protect may see the host MAC")

    def _nft_rules(self, links: dict[str, dict]) -> bool:
        nft = "nft" if not self._managed else _tool("nft")
        if not nft:
            return False
        self._run([nft, "delete", "table", "arp", _NFT_TABLE])
        if not links:
            return True
        lines = [
            f"table arp {_NFT_TABLE} {{",
            "  chain input {",
            "    type filter hook input priority 0; policy accept;",
        ]
        for ip, entry in sorted(links.items()):
            lines.append(f'    iifname "{entry["interface"]}" arp daddr ip {ip} drop')
            lines.append(f'    iifname "{entry["link"]}" arp daddr ip != {ip} drop')
        lines += ["  }", "}", ""]
        handle, script = tempfile.mkstemp(suffix=".nft")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write("\n".join(lines))
            return self._run([nft, "-f", script]).returncode == 0
        finally:
            try:
                os.unlink(script)
            except OSError:
                pass

    def _load(self) -> None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
        if isinstance(payload, dict):
            self._entries = {str(ip): value for ip, value in payload.items() if isinstance(value, dict)}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._entries, indent=2), encoding="utf-8")


def place_camera(
    camera,
    previous,
    interfaces: list[dict],
    book: AddressBook,
    *,
    probe=None,
    present: set[str] | None = None,
    wireless=None,
    hardware=None,
) -> None:
    """Reserve the camera's address. Loopback stays on 127.0.0.1."""
    if _loopback(camera, interfaces):
        if previous is not None and previous.virtual_ip:
            book.release(previous.listen_ip)
        if camera.listen_ip in {"", "0.0.0.0"}:
            camera.listen_ip = "127.0.0.1"
        camera.virtual_ip = False
        camera.service_port = 0
        camera.mac = ""
        camera.link = ""
        return
    info = _interface(camera, interfaces)
    if info is None:
        raise ValueError("choose a broadcast network")
    prefix = int(info.get("prefix") or 24)
    primary = str(info["ip"])
    network = IPv4Network(f"{primary}/{prefix}", strict=False)
    if (wireless or is_wireless)(camera.interface):
        link, mac = "", (hardware or hardware_address)(camera.interface)
    else:
        link, mac = link_name(camera.id), virtual_mac(camera.id)
    probe = probe or _probe

    requested = str(getattr(camera, "requested_ip", "") or "").strip()
    held = (
        previous.listen_ip
        if previous is not None
        and previous.virtual_ip
        and previous.interface == camera.interface
        and previous.listen_ip not in {primary, "0.0.0.0", ""}
        and not str(previous.listen_ip).startswith("127.")
        else ""
    )
    if requested:
        address = IPv4Address(requested)
        if address not in network or address in {network.network_address, network.broadcast_address}:
            raise ValueError(f"{requested} is not a usable address on {camera.interface} ({network})")
        if requested == primary:
            raise ValueError(f"{requested} is the host's own address")
        owner = book.owner(requested)
        if owner and owner != camera.id:
            raise ValueError(f"{requested} is already used by another camera")
        if requested != held and not owner and probe(requested):
            raise ValueError(f"{requested} already answers on the network")
        chosen = requested
    else:
        chosen = held

    if previous is not None and previous.virtual_ip and previous.listen_ip and previous.listen_ip != chosen:
        book.release(previous.listen_ip)
    if chosen:
        book.ensure(camera.interface, chosen, prefix, camera.id, link=link, mac=mac)
    else:
        chosen = book.allocate(camera.interface, primary, prefix, camera.id, probe=probe, present=present, link=link, mac=mac)
    camera.listen_ip = chosen
    camera.virtual_ip = True
    camera.prefix_length = prefix
    camera.mac = mac
    camera.link = link
    camera.service_port = (previous.service_port if previous is not None and previous.listen_ip == chosen else 0) or 80


def _loopback(camera, interfaces: list[dict]) -> bool:
    if str(camera.listen_ip).startswith("127."):
        return True
    for item in interfaces:
        if item.get("name") == camera.interface and str(item.get("ip", "")).startswith("127."):
            return True
    return not camera.interface and camera.listen_ip in {"", "0.0.0.0"}


def _interface(camera, interfaces: list[dict]) -> dict | None:
    named = [item for item in interfaces if item.get("name") == camera.interface and item.get("ip")]
    real = [item for item in named if not str(item["ip"]).startswith("127.")]
    return (real or named or [None])[0]


def _candidates(primary_ip: str, prefix: int) -> list[IPv4Address]:
    network = IPv4Network(f"{primary_ip}/{prefix}", strict=False)
    base = int(IPv4Address(primary_ip)) & 0xFFFFFF00
    found = []
    for host in range(250, 19, -1):
        candidate = IPv4Address(base + host)
        if candidate in network and str(candidate) != primary_ip:
            found.append(candidate)
    return found


def _probe(ip: str) -> bool:
    """A device that ignores ping still answers ARP, so check the neighbour table too."""
    try:
        completed = subprocess.run(["ping", "-c", "1", "-W", "1", ip], check=False, capture_output=True, text=True, timeout=2)
        if completed.returncode == 0:
            return True
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        rows = Path("/proc/net/arp").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return False
    for row in rows:
        parts = row.split()
        if len(parts) >= 4 and parts[0] == ip and parts[2] != "0x0" and parts[3] != "00:00:00:00:00:00":
            return True
    return False
