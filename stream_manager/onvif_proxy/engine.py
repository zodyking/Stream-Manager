"""go2rtc process and localhost API. The engine UI is not exposed."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from pathlib import Path

log = logging.getLogger("onvif_proxy.engine")
stream_log = logging.getLogger("go2rtc")


def find_binary(explicit: str) -> str:
    candidates = [
        explicit,
        os.environ.get("GO2RTC_BIN", ""),
        str(Path.cwd() / "bin" / "go2rtc.exe"),
        str(Path.cwd() / "bin" / "go2rtc"),
        "/usr/bin/go2rtc",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    found = shutil.which("go2rtc") or shutil.which("go2rtc.exe")
    return found or ""


def redact(src: str) -> str:
    import re

    return re.sub(r"//[^/\s@]+@", "//***@", src)


def source_needle(src: str) -> str:
    """The input text ffmpeg is given, without the named templates."""
    raw = src[7:] if src.startswith("ffmpeg:") else src
    return raw.split("#", 1)[0]


def producer_pids(commands: list[tuple[int, str]], needle: str) -> list[int]:
    """FFmpeg processes whose command line is still the previous source."""
    if len(needle) < 4:
        return []
    return [pid for pid, command in commands if "ffmpeg" in command.lower() and needle in command]


def child_commands(parent: int) -> list[tuple[int, str]]:
    """Command lines of processes started by go2rtc."""
    proc = Path("/proc")
    if proc.is_dir():
        found: list[tuple[int, str]] = []
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat = (entry / "stat").read_text(encoding="utf-8", errors="replace")
                fields = stat.rsplit(")", 1)[-1].split()
                if len(fields) < 2 or int(fields[1]) != parent:
                    continue
                command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
            except (OSError, ValueError):
                continue
            found.append((int(entry.name), command))
        return found
    if os.name != "nt":
        return []
    script = (
        "Get-CimInstance Win32_Process -Filter \"ParentProcessId="
        + str(int(parent))
        + "\" | ForEach-Object { $_.ProcessId.ToString() + \"`t\" + $_.CommandLine }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in completed.stdout.splitlines():
        pid, separator, command = line.partition("\t")
        if separator and pid.strip().isdigit():
            found.append((int(pid.strip()), command))
    return found


class Go2RtcEngine:
    def __init__(self, binary: str, api_port: int, rtsp_port: int, data_dir: Path) -> None:
        self.binary = binary
        self.api_port = api_port
        self.rtsp_port = rtsp_port
        self.data_dir = data_dir
        self.process: subprocess.Popen | None = None
        self.running = False
        self.error = ""
        self._known: set[str] = set()
        self._sources: dict[str, str] = {}
        self.templates: dict[str, str] = {}
        self._loaded: dict[str, str] = {}
        self.recent: deque[str] = deque(maxlen=60)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def write_config(self) -> Path:
        path = self.data_dir / "go2rtc.yaml"
        path.write_text(
            "\n".join(
                [
                    "api:",
                    f'  listen: "127.0.0.1:{self.api_port}"',
                    "  origin: '*'",
                    "rtsp:",
                    f'  listen: ":{self.rtsp_port}"',
                    "webrtc:",
                    '  listen: ""',
                    "ffmpeg:",
                    "  bin: ffmpeg",
                    *[
                        f"  {name}: {json.dumps(body)}"
                        for name, body in sorted(self.templates.items())
                    ],
                    "log:",
                    "  level: info",
                    "streams: {}",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        return path

    def start(self) -> None:
        if not self.binary:
            self.running = False
            self.error = "go2rtc binary was not found; ONVIF URLs are still advertised"
            log.warning(self.error)
            return
        config = self.write_config()
        try:
            self.process = subprocess.Popen(
                [self.binary, "-config", str(config)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self.error = str(exc)
            self.running = False
            return
        threading.Thread(target=self._pump, args=(self.process,), daemon=True).start()
        self.error = ""
        self.running = True
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.running = False
                self.error = "The stream engine exited before it could publish the camera."
                return
            if self.ping():
                return
            time.sleep(0.1)
        self.error = "The stream engine did not open in time."

    def _pump(self, process: subprocess.Popen) -> None:
        """Forward go2rtc and ffmpeg output to the add-on log."""
        for raw in iter(process.stdout.readline, b""):
            line = redact(raw.decode("utf-8", "replace").rstrip())
            if not line:
                continue
            self.recent.append(line)
            if "[rtsp] listen" in line and " ERR " in line:
                self.error = f"RTSP port {self.rtsp_port} is used by another program, so no stream can play. Change rtsp_port in the add-on options."
            level = logging.WARNING if any(tag in line for tag in (" WRN ", " ERR ", " FTL ", "error", "Error")) else logging.INFO
            stream_log.log(level, "%s", line)
        process.stdout.close()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.running = False
        self._sources.clear()

    def ping(self) -> bool:
        try:
            with urllib.request.urlopen(self.base + "/api/streams", timeout=2) as response:
                return response.status == 200
        except (urllib.error.URLError, TimeoutError, OSError):
            return False

    def _request(self, method: str, name: str, src: str | None) -> tuple[int, str]:
        query = {"name": name}
        if src is not None:
            query["src"] = src
        url = self.base + "/api/streams?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            return 0, str(exc)

    def ensure_templates(self, templates: dict[str, str]) -> bool:
        """Restart go2rtc when a new ffmpeg template has to be loaded."""
        fresh = any(self._loaded.get(key) != value for key, value in templates.items())
        self.templates.update(templates)
        if not fresh or not self.running:
            return False
        self.stop()
        self.start()
        if self.running:
            self._loaded = dict(self.templates)
            return True
        return False

    def upsert(self, name: str, src: str) -> str:
        if not self.binary:
            return self.error or "engine not installed"
        if self.process and self.process.poll() is not None:
            self.running = False
            self.error = "go2rtc exited"
            return self.error
        log.info("stream %s source %s", name, redact(src))
        previous = self._sources.get(name)
        status, body = self._request("PATCH", name, src)
        if status == 400:
            status, body = self._request("PUT", name, src)
        if status in {0, 400}:
            if "Connection refused" in body or body.startswith("<urlopen error"):
                body = "The stream engine is not running, so this camera was saved but not published."
            self.error = body[:300] or f"engine HTTP {status}"
            return self.error
        self._known.add(name)
        self._sources[name] = src
        self.error = ""
        if previous and previous != src:
            self._release(previous)
        return ""

    def _commands(self) -> list[tuple[int, str]]:
        if self.process is None or self.process.poll() is not None:
            return []
        return child_commands(self.process.pid)

    def _stop(self, pid: int) -> None:
        os.kill(pid, signal.SIGTERM)

    def _release(self, previous: str) -> None:
        """End the ffmpeg that is still publishing the old source.

        go2rtc remembers the new address but leaves the running encoder alone.
        Stopping that process makes it reconnect, and viewers keep the same stream.
        """
        needle = source_needle(previous)
        try:
            commands = self._commands()
        except OSError:
            log.warning("could not list ffmpeg processes")
            return
        for pid in producer_pids(commands, needle):
            log.info("stopping ffmpeg %s so the published stream can change source", pid)
            try:
                self._stop(pid)
            except OSError:
                log.warning("could not stop ffmpeg %s", pid)

    def remove(self, name: str) -> None:
        if not self.binary:
            return
        self._request("DELETE", name, None)
        self._known.discard(name)
        self._sources.pop(name, None)


class RecordingEngine:
    """Test double that records the active producer without a process."""

    def __init__(self) -> None:
        self.streams: dict[str, str] = {}
        self.binary = "recording"
        self.running = True
        self.error = ""
        self.templates: dict[str, str] = {}
        self.recent: deque[str] = deque(maxlen=60)

    def start(self) -> None:
        self.running = True

    def stop(self) -> None:
        self.running = False

    def ping(self) -> bool:
        return True

    def ensure_templates(self, templates: dict[str, str]) -> bool:
        self.templates.update(templates)
        return False

    def upsert(self, name: str, src: str) -> str:
        self.streams[name] = src
        return ""

    def remove(self, name: str) -> None:
        self.streams.pop(name, None)
