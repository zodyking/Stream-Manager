"""List IPv4 interfaces. Linux ioctl and Windows PowerShell are both optional."""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess


def list_interfaces() -> list[dict[str, str]]:
    try:
        if os.name == "nt":
            found = _windows()
        else:
            found = _linux()
    except Exception as exc:  # pragma: no cover - defensive fallback
        return [{"name": "loopback", "ip": "127.0.0.1", "error": str(exc)}]
    if not any(item["ip"] == "127.0.0.1" for item in found):
        found.append({"name": "loopback", "ip": "127.0.0.1"})
    return found


def _linux() -> list[dict[str, str]]:
    import fcntl

    root = "/sys/class/net"
    if not os.path.isdir(root):
        return [{"name": "loopback", "ip": "127.0.0.1", "error": "interface sysfs is unavailable"}]
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: list[dict[str, str]] = []
    try:
        for name in sorted(os.listdir(root)):
            if name.startswith("smv"):
                continue
            try:
                packed = struct.pack("256s", name.encode("utf-8")[:15])
                info = fcntl.ioctl(sock.fileno(), 0x8915, packed)
                ip = socket.inet_ntoa(info[20:24])
            except OSError:
                continue
            if ip:
                prefix = "24"
                try:
                    mask_info = fcntl.ioctl(sock.fileno(), 0x891B, packed)
                    mask = socket.inet_ntoa(mask_info[20:24])
                    prefix = str(sum(bin(int(part)).count("1") for part in mask.split(".")))
                except OSError:
                    prefix = "24"
                wireless = os.path.exists(os.path.join(root, name, "wireless")) or os.path.exists(os.path.join(root, name, "phy80211"))
                found.append({"name": name, "ip": ip, "prefix": prefix, "wireless": wireless})
    finally:
        sock.close()
    return found


def _windows() -> list[dict[str, str]]:
    command = (
        "Get-NetIPAddress -AddressFamily IPv4 | "
        "Select-Object InterfaceAlias,IPAddress,PrefixLength | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            check=False,
            capture_output=True,
            text=True,
            timeout=12,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [{"name": "loopback", "ip": "127.0.0.1", "error": str(exc)}]
    text = (completed.stdout or "").strip()
    if not text:
        return [{"name": "loopback", "ip": "127.0.0.1", "error": (completed.stderr or "no interfaces")[:200]}]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [{"name": "loopback", "ip": "127.0.0.1", "error": "could not parse interface list"}]
    rows = payload if isinstance(payload, list) else [payload]
    found: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        ip = str(row.get("IPAddress") or "")
        name = str(row.get("InterfaceAlias") or "interface")
        if ip:
            found.append({"name": name, "ip": ip, "prefix": str(row.get("PrefixLength") or 24)})
    return found
