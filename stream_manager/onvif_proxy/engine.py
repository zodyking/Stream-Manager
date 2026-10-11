"""go2rtc process and localhost API. The engine UI is not exposed."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger("onvif_proxy.engine")


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
        self.templates: dict[str, str] = {}
        self._loaded: dict[str, str] = {}

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
        log_path = self.data_dir / "go2rtc.log"
        handle = log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                [self.binary, "-config", str(config)],
                stdout=handle,
                stderr=subprocess.STDOUT,
            )
        except OSError as exc:
            self.error = str(exc)
            self.running = False
            handle.close()
            return
        self.error = ""
        self.running = True

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        self.running = False

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
        status, body = self._request("PATCH", name, src)
        if status == 400:
            status, body = self._request("PUT", name, src)
        if status in {0, 400}:
            self.error = body[:300] or f"engine HTTP {status}"
            return self.error
        self._known.add(name)
        self.error = ""
        return ""

    def remove(self, name: str) -> None:
        if not self.binary:
            return
        self._request("DELETE", name, None)
        self._known.discard(name)


class RecordingEngine:
    """Test double that records the active producer without a process."""

    def __init__(self) -> None:
        self.streams: dict[str, str] = {}
        self.binary = "recording"
        self.running = True
        self.error = ""
        self.templates: dict[str, str] = {}

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
