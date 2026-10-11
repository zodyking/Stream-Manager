"""Reserve one IPv4 address per camera on the broadcast interface."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path


def _ip_bin() -> str:
    for candidate in ("/sbin/ip", "/usr/sbin/ip"):
        if os.path.isfile(candidate):
            return candidate
    return shutil.which("ip") or ""


def _default_run(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=False, capture_output=True, text=True, timeout=3)


class AddressBook:
    def __init__(self, path: Path, run=None) -> None:
        self.path = path
        self._managed = run is None
        self._run = run or _default_run
        self._entries: dict[str, dict] = {}
        self._load()

    def restore(self) -> None:
        for ip, entry in list(self._entries.items()):
            self._add(str(entry.get("interface") or ""), ip, int(entry.get("prefix") or 24))

    def release(self, ip: str) -> None:
        entry = self._entries.pop(ip, None)
        if entry:
            self._delete(str(entry.get("interface") or ""), ip, int(entry.get("prefix") or 24))
            self._save()

    def ensure(self, interface: str, ip: str, prefix: int, camera_id: str) -> None:
        self._add(interface, ip, prefix)
        self._entries[ip] = {"interface": interface, "prefix": prefix, "camera_id": camera_id}
        self._save()

    def allocate(
        self,
        interface: str,
        primary_ip: str,
        prefix: int,
        camera_id: str,
        *,
        probe,
        present: set[str] | None = None,
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
            self.ensure(interface, text, prefix, camera_id)
            return text
        raise ValueError(f"no free address on {interface}")

    def _add(self, interface: str, ip: str, prefix: int) -> None:
        if not interface or not ip:
            raise ValueError("this host cannot add an address on the broadcast network")
        binary = "ip" if not self._managed else _ip_bin()
        if not binary:
            raise ValueError("this host cannot add an address on the broadcast network")
        completed = self._run([binary, "addr", "add", f"{ip}/{prefix}", "dev", interface])
        error = (completed.stderr or "").lower()
        if completed.returncode and "exist" not in error:
            raise ValueError((completed.stderr or "could not add the address").strip())

    def _delete(self, interface: str, ip: str, prefix: int) -> None:
        binary = "ip" if not self._managed else _ip_bin()
        if not binary or not interface:
            return
        self._run([binary, "addr", "del", f"{ip}/{prefix}", "dev", interface])

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


def place_camera(camera, previous, interfaces: list[dict], book: AddressBook, *, probe=None, present: set[str] | None = None) -> None:
    """Give a real broadcast interface its own address. Loopback stays on 127.0.0.1."""
    if _loopback(camera, interfaces):
        if previous is not None and previous.virtual_ip:
            book.release(previous.listen_ip)
        if camera.listen_ip in {"", "0.0.0.0"}:
            camera.listen_ip = "127.0.0.1"
        camera.virtual_ip = False
        camera.service_port = 0
        return
    info = _interface(camera, interfaces)
    if info is None:
        raise ValueError("choose a broadcast network")
    prefix = int(info.get("prefix") or 24)
    primary = str(info["ip"])
    if (
        previous is not None
        and previous.virtual_ip
        and previous.interface == camera.interface
        and previous.listen_ip
        and previous.listen_ip not in {primary, "0.0.0.0"}
        and not str(previous.listen_ip).startswith("127.")
    ):
        book.ensure(camera.interface, previous.listen_ip, prefix, camera.id)
        camera.listen_ip = previous.listen_ip
        camera.virtual_ip = True
        camera.prefix_length = prefix
        camera.service_port = previous.service_port or 80
        return
    if previous is not None and previous.virtual_ip:
        book.release(previous.listen_ip)
    camera.listen_ip = book.allocate(
        camera.interface,
        primary,
        prefix,
        camera.id,
        probe=probe or _ping,
        present=present,
    )
    camera.virtual_ip = True
    camera.prefix_length = prefix
    camera.service_port = 80


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


def _ping(ip: str) -> bool:
    try:
        completed = subprocess.run(["ping", "-c", "1", "-W", "1", ip], check=False, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0
