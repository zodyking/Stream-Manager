"""Load options, the data directory, and the camera store."""

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from .models import Camera, camera_from_dict, camera_to_dict


@dataclass
class Settings:
    data_dir: Path
    ui_port: int
    onvif_port: int
    rtsp_port: int
    go2rtc_api_port: int
    poll_seconds: int
    ha_url: str
    ha_token: str
    supervisor: bool
    dev: bool
    go2rtc_bin: str
    rtsp_port_requested: int = 0


def port_free(port: int, host: str = "") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def pick_port(preferred: int, fallbacks: tuple[int, ...], host: str = "", check=port_free) -> int:
    """The first free port, so a port held by another add-on does not disable the stream engine."""
    for port in (preferred, *fallbacks):
        if check(port, host):
            return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return sock.getsockname()[1]


def _int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def default_data_dir() -> Path:
    env = os.environ.get("HASP_DATA_DIR")
    if env:
        return Path(env)
    if os.environ.get("SUPERVISOR_TOKEN") and Path("/data").is_dir():
        return Path("/data")
    return Path.cwd() / "data"


def load_settings() -> Settings:
    data_dir = default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    options = _read_json(data_dir / "options.json")
    local = _read_json(Path.cwd() / "config.local.json")
    if not local:
        local = _read_json(data_dir / "config.local.json")
    merged = {**options, **local}
    supervisor = bool(os.environ.get("SUPERVISOR_TOKEN"))
    token = os.environ.get("SUPERVISOR_TOKEN") or str(merged.get("ha_token") or os.environ.get("HASP_HA_TOKEN") or "")
    ha_url = str(merged.get("ha_url") or os.environ.get("HASP_HA_URL") or "")
    if supervisor:
        ha_url = "http://supervisor/core"
    dev_default = "0" if supervisor else "1"
    go2rtc_bin = os.environ.get("GO2RTC_BIN") or str(merged.get("go2rtc_bin") or "")
    return Settings(
        data_dir=data_dir,
        ui_port=_int(os.environ.get("HASP_UI_PORT") or merged.get("ui_port"), 8099),
        onvif_port=_int(os.environ.get("HASP_ONVIF_PORT") or merged.get("onvif_port"), 8080),
        rtsp_port=_int(os.environ.get("HASP_RTSP_PORT") or merged.get("rtsp_port"), 8554),
        go2rtc_api_port=_int(os.environ.get("HASP_GO2RTC_PORT") or merged.get("go2rtc_api_port"), 19841),
        poll_seconds=max(2, _int(merged.get("poll_seconds"), 5)),
        ha_url=ha_url.rstrip("/"),
        ha_token=token,
        supervisor=supervisor,
        dev=os.environ.get("HASP_DEV", dev_default) == "1",
        go2rtc_bin=go2rtc_bin,
    )


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.cameras: list[Camera] = []
        self.load()

    def load(self) -> None:
        raw = _read_json(self.path)
        items = raw.get("cameras") if isinstance(raw, dict) else None
        cameras: list[Camera] = []
        if isinstance(items, list):
            for item in items:
                try:
                    cameras.append(camera_from_dict(item))
                except ValueError:
                    continue
        self.cameras = cameras

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "cameras": [camera_to_dict(camera) for camera in self.cameras]}
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    def get(self, camera_id: str) -> Camera | None:
        for camera in self.cameras:
            if camera.id == camera_id:
                return camera
        return None

    def upsert(self, camera: Camera) -> None:
        for index, current in enumerate(self.cameras):
            if current.id == camera.id:
                self.cameras[index] = camera
                self.save()
                return
        self.cameras.append(camera)
        self.save()

    def delete(self, camera_id: str) -> bool:
        before = len(self.cameras)
        self.cameras = [camera for camera in self.cameras if camera.id != camera_id]
        if len(self.cameras) == before:
            return False
        self.save()
        return True
