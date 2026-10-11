"""Apply rule decisions to a stable go2rtc stream name."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import quote

from .engine import Go2RtcEngine
from .models import Camera, Endpoints, OutputSettings, Source, endpoints_for
from .rules import Decision, decide, selected_source

log = logging.getLogger("onvif_proxy.streams")


def ffmpeg_still(target: str) -> str:
    return f'ffmpeg:{target}#input=-loop 1 -framerate 5 -i "{{input}}"#video=h264'


def with_source_bind(src: str, source_ip: str) -> str:
    """Open RTSP and remote images from the listening address."""
    if not source_ip or not src.startswith("ffmpeg:"):
        return src
    target = src[len("ffmpeg:") :].split("#", 1)[0]
    remote_http = target.startswith(("http://", "https://")) and "127.0.0.1" not in target and "localhost" not in target
    if not (target.startswith(("rtsp://", "rtsps://")) or remote_http):
        return src
    for needle in ("-i {input}", '-i "{input}"'):
        if needle in src:
            return src.replace(needle, f"-localaddr {source_ip} {needle}", 1)
    if target.startswith(("rtsp://", "rtsps://")):
        template = (
            "-fflags nobuffer -flags low_delay -timeout 5000000 "
            "-user_agent go2rtc/ffmpeg -rtsp_flags prefer_tcp "
            f"-localaddr {source_ip} -i {{input}}"
        )
    else:
        template = f"-fflags nobuffer -flags low_delay -localaddr {source_ip} -i {{input}}"
    return src + "#input=" + template


def with_output(src: str, output: OutputSettings) -> str:
    """Encode the published stream to the ONVIF profile instead of passing the source through."""
    if src.endswith("#video=h264"):
        src = src[: -len("#video=h264")]
    profile = output.h264_profile.casefold()
    suffix = (
        "#video=-c:v libx264"
        f" -g {output.gov_length}"
        f" -r {output.framerate}"
        f" -profile:v {profile}"
        " -level:v 4.1 -preset:v superfast -tune:v zerolatency -pix_fmt:v yuv420p"
        f"#width={output.width}#height={output.height}#bitrate={output.bitrate_kbps}k"
    )
    if src.startswith("ffmpeg:"):
        return src + suffix
    return "ffmpeg:" + src + suffix


def onvif_source_url(source: Source) -> str:
    host = source.host
    port = source.port or 80
    if source.username:
        user = quote(source.username, safe="")
        password = quote(source.password, safe="")
        return f"onvif://{user}:{password}@{host}:{port}"
    return f"onvif://{host}:{port}"


def producer_for(source: Source, *, data_dir: Path, ui_port: int, ha_mode: str | None, ha_error: str | None) -> tuple[str | None, str | None]:
    """Return (go2rtc src, error). The RTSP name is chosen by the caller."""
    if source.type == "rtsp":
        return source.url, None
    if source.type == "onvif":
        return onvif_source_url(source), None
    if source.type == "image":
        if source.image_path:
            path = Path(source.image_path)
            if not path.is_absolute():
                path = data_dir / source.image_path
            return ffmpeg_still(path.as_posix()), None
        return ffmpeg_still(source.image_url), None
    if source.type == "ha_camera":
        if ha_error:
            return None, ha_error
        if ha_mode == "mjpeg":
            return f"ffmpeg:http://127.0.0.1:{ui_port}/internal/ha/{source.entity_id}/mjpeg#video=h264", None
        if ha_mode == "hls":
            return (
                f"ffmpeg:http://127.0.0.1:{ui_port}/internal/ha/{source.entity_id}/playlist.m3u8#video=h264",
                None,
            )
        return None, ha_error or "Home Assistant camera source is not resolved"
    return None, f"unsupported source type {source.type}"


class StreamManager:
    def __init__(self, engine: Go2RtcEngine, ui_port: int, onvif_port: int, rtsp_port: int, data_dir: Path) -> None:
        self.engine = engine
        self.ui_port = ui_port
        self.onvif_port = onvif_port
        self.rtsp_port = rtsp_port
        self.data_dir = data_dir
        self.runtime: dict[str, dict] = {}
        self._applied: dict[str, str] = {}
        self._lock = asyncio.Lock()

    def urls(self, camera: Camera) -> Endpoints:
        return endpoints_for(camera, self.onvif_port, self.rtsp_port)

    async def sync(self, cameras: list[Camera], states: dict[str, str], ha) -> None:
        async with self._lock:
            live = {camera.id for camera in cameras if camera.enabled}
            for camera_id in list(self._applied):
                if camera_id not in live:
                    await asyncio.to_thread(self.engine.remove, camera_id)
                    self._applied.pop(camera_id, None)
                    self.runtime.pop(camera_id, None)
            for camera in cameras:
                await self._apply_one(camera, states, ha)

    async def _apply_one(self, camera: Camera, states: dict[str, str], ha) -> None:
        decision: Decision = decide(camera, states)
        source = selected_source(camera, decision)
        urls = self.urls(camera)
        record = {
            "camera_id": camera.id,
            "enabled": camera.enabled,
            "reason": decision.reason,
            "rule_id": decision.rule_id,
            "source_id": decision.source_id,
            "source_name": source.name if source else "",
            "source_type": source.type if source else "",
            "source_kind": source.kind() if source else "",
            "rtsp_url": urls.stream,
            "onvif_url": urls.device,
            "snapshot_url": urls.snapshot,
            "error": "",
            "engine_error": "",
        }
        if not camera.enabled or source is None:
            record["error"] = "" if decision.reason == "disabled" else "no source is available"
            self.runtime[camera.id] = record
            return
        ha_mode = None
        ha_error = None
        if source.type == "ha_camera":
            playback = await ha.playback_for_entity(source.entity_id)
            ha_mode = playback.mode
            ha_error = playback.error
            record["ha_mode"] = ha_mode or ""
        src, error = producer_for(
            source,
            data_dir=self.data_dir,
            ui_port=self.ui_port,
            ha_mode=ha_mode,
            ha_error=ha_error,
        )
        if error or not src:
            record["error"] = error or "source could not be prepared"
            if self._applied.get(camera.id):
                await asyncio.to_thread(self.engine.remove, camera.id)
                self._applied.pop(camera.id, None)
            self.runtime[camera.id] = record
            return
        src = with_source_bind(with_output(src, camera.output), camera.source_ip)
        record["producer"] = src
        if self._applied.get(camera.id) != src:
            engine_error = await asyncio.to_thread(self.engine.upsert, camera.id, src)
            if engine_error:
                record["engine_error"] = engine_error
            else:
                self._applied[camera.id] = src
        self.runtime[camera.id] = record
