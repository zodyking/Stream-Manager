"""Apply rule decisions to a stable go2rtc stream name."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from .compositor import (
    CanvasBoard,
    canvas_video_args,
    compositor_input,
    helper_stream_name,
    layers_for,
)
from .engine import Go2RtcEngine
from .models import Camera, Endpoints, OutputSettings, Source, endpoints_for
from .rules import Decision, cycle_decision, decide, selected_source

log = logging.getLogger("onvif_proxy.streams")

_TEMPLATES: dict[str, str] = {}
_AUDIO_CACHE: dict[str, bool] = {}
_INPUT_KEY = re.compile(r"#input=[^#]*")


def ffmpeg_templates() -> dict[str, str]:
    return dict(_TEMPLATES)


def _template(body: str) -> str:
    """Store ffmpeg arguments under a name. go2rtc rejects spaces in a source URL."""
    name = "sm" + hashlib.sha1(body.encode()).hexdigest()[:10]
    _TEMPLATES[name] = body
    return name


def ffmpeg_still(target: str) -> str:
    name = _template("-loop 1 -framerate 5 -i {input}")
    return f"ffmpeg:{target}#input={name}#video=h264"


def ffmpeg_media(target: str) -> str:
    """Play a video or GIF in real time and loop it."""
    name = _template("-stream_loop -1 -re -fflags +genpts -i {input}")
    return f"ffmpeg:{target}#input={name}#video=h264"


def with_source_bind(src: str, source_ip: str) -> str:
    """Open RTSP and remote images from the listening address."""
    if not source_ip or not src.startswith("ffmpeg:"):
        return src
    target = src[len("ffmpeg:") :].split("#", 1)[0]
    remote_http = target.startswith(("http://", "https://")) and "127.0.0.1" not in target and "localhost" not in target
    if not (target.startswith(("rtsp://", "rtsps://")) or remote_http):
        return src
    if target.startswith(("rtsp://", "rtsps://")):
        body = (
            "-fflags nobuffer -flags low_delay -timeout {timeout} "
            "-user_agent go2rtc/ffmpeg -rtsp_flags prefer_tcp "
            f"-localaddr {source_ip} -i {{input}}"
        )
    else:
        body = f"-fflags nobuffer -flags low_delay -localaddr {source_ip} -i {{input}}"
    name = _template(body)
    if "#input=" in src:
        return _INPUT_KEY.sub("#input=" + name, src, count=1)
    return src + "#input=" + name


def with_output(src: str, output: OutputSettings, *, audio: bool = False) -> str:
    """Encode the published stream to the ONVIF profile instead of passing the source through.

    Audio is a separate encoder and is added only when the source already has a
    sound track. Mapping a missing track, or advertising one that is not in the
    stream, makes a looping video or GIF sit on the first frame.
    """
    if src.endswith("#video=h264"):
        src = src[: -len("#video=h264")]
    profile = output.h264_profile.casefold()
    video = _template(
        "-c:v libx264"
        f" -g {output.gov_length}"
        f" -r {output.framerate}"
        f" -profile:v {profile}"
        " -level:v 4.1 -preset:v superfast -tune:v zerolatency -pix_fmt:v yuv420p"
        " -bf 0 -x264-params sliced-threads=0:slices=1:repeat-headers=1:scenecut=0"
    )
    suffix = f"#video={video}#width={output.width}#height={output.height}#bitrate={output.bitrate_kbps}k"
    if audio:
        suffix += "#audio=" + _template("-c:a aac -b:a 128k -ac 2 -ar 48000")
    if src.startswith("ffmpeg:"):
        return src + suffix
    return "ffmpeg:" + src + suffix


def _media_path(source: Source, data_dir: Path) -> Path | None:
    if source.type != "media" or not source.image_path:
        return None
    path = Path(source.image_path)
    if not path.is_absolute():
        path = data_dir / source.image_path
    return path


def source_has_audio(source: Source, data_dir: Path) -> bool:
    """True when an uploaded video file contains an audio stream."""
    path = _media_path(source, data_dir)
    if path is None or not path.is_file():
        return False
    key = f"{path}:{path.stat().st_mtime_ns}"
    cached = _AUDIO_CACHE.get(key)
    if cached is not None:
        return cached
    binary = shutil.which("ffprobe")
    found = False
    if binary:
        try:
            result = subprocess.run(
                [
                    binary,
                    "-v",
                    "error",
                    "-select_streams",
                    "a:0",
                    "-show_entries",
                    "stream=codec_type",
                    "-of",
                    "csv=p=0",
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            found = "audio" in (result.stdout or "")
        except (OSError, subprocess.TimeoutExpired):
            found = False
    _AUDIO_CACHE[key] = found
    return found


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
    if source.type == "media":
        path = _media_path(source, data_dir)
        if path is None or not path.is_file():
            return None, "the video or GIF file is missing"
        return ffmpeg_media(path.as_posix()), None
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
        self.canvas = CanvasBoard(data_dir, ui_port, rtsp_port)

    def urls(self, camera: Camera) -> Endpoints:
        return endpoints_for(camera, self.onvif_port, self.rtsp_port)

    async def sync(self, cameras: list[Camera], states: dict[str, str], ha) -> None:
        async with self._lock:
            live = {camera.id for camera in cameras if camera.enabled}
            for camera_id in list(self._applied):
                if camera_id not in live:
                    await asyncio.to_thread(self.engine.remove, camera_id)
                    for helper in self.canvas.drop(camera_id):
                        await asyncio.to_thread(self.engine.remove, helper)
                    self._applied.pop(camera_id, None)
                    self.runtime.pop(camera_id, None)
            prepared = [await self._prepare(camera, states, ha) for camera in cameras]
            restarted = await asyncio.to_thread(self.engine.ensure_templates, ffmpeg_templates())
            if restarted:
                self._applied.clear()
            for record, src in prepared:
                await self._commit(record, src)

    def close(self) -> list[str]:
        return self.canvas.drop_all()

    def _canvas_source(self, camera: Camera, source_id: str) -> str:
        video = _template(canvas_video_args(camera.output))
        incoming = _template(compositor_input(camera, source_id))
        return f"ffmpeg:canvas#input={incoming}#video={video}"

    async def _prepare_canvas(self, camera: Camera, ha, decision: Decision, source) -> tuple[dict, str | None]:
        urls = self.urls(camera)
        record = {
            "camera_id": camera.id,
            "enabled": camera.enabled,
            "reason": decision.reason,
            "rule_id": decision.rule_id,
            "source_id": decision.source_id,
            "source_name": source.name if source else "",
            "source_type": "canvas",
            "source_kind": "canvas",
            "rtsp_url": urls.stream,
            "onvif_url": urls.device,
            "snapshot_url": urls.snapshot,
            "error": "",
            "engine_error": "",
            "has_audio": False,
            "designed": True,
            "tiles": {},
        }
        if not camera.enabled:
            for helper in self.canvas.drop(camera.id):
                await asyncio.to_thread(self.engine.remove, helper)
            return record, None
        ha_modes: dict[str, str | None] = {}
        helpers: dict[str, str] = {}
        helper_names: set[str] = set()
        layout_id = decision.source_id or ""
        for layer in layers_for(camera, layout_id):
            if layer.type != "source":
                continue
            source = camera.source_map().get(layer.source_id)
            if source is None:
                continue
            if source.type == "ha_camera":
                playback = await ha.playback_for_entity(source.entity_id)
                ha_modes[layer.source_id] = playback.mode
                if playback.error:
                    record["error"] = playback.error
            if source.type == "onvif":
                src, error = producer_for(
                    source,
                    data_dir=self.data_dir,
                    ui_port=self.ui_port,
                    ha_mode=None,
                    ha_error=None,
                )
                name = helper_stream_name(camera.id, layer.id)
                helper_names.add(name)
                if src and not error:
                    await asyncio.to_thread(self.engine.upsert, name, with_output(src, camera.output, audio=False))
                    helpers[layer.id] = f"rtsp://127.0.0.1:{self.rtsp_port}/{name}"
                elif error:
                    record["error"] = error
        for helper in self.canvas.note_helpers(camera.id, helper_names):
            await asyncio.to_thread(self.engine.remove, helper)
        self.canvas.sync_camera(camera, ha_modes=ha_modes, helpers=helpers, source_id=layout_id)
        record["tiles"] = self.canvas.modes(camera.id)
        src = self._canvas_source(camera, layout_id)
        record["producer"] = src
        return record, src

    async def _prepare(self, camera: Camera, states: dict[str, str], ha) -> tuple[dict, str | None]:
        decision: Decision = decide(camera, states)
        if decision.reason in {"fallback", "no_source"}:
            cycled = cycle_decision(camera, time.monotonic())
            if cycled is not None:
                decision = cycled
        source = selected_source(camera, decision)
        if camera.enabled and source is not None and layers_for(camera, source.id):
            return await self._prepare_canvas(camera, ha, decision, source)
        for helper in self.canvas.drop(camera.id):
            await asyncio.to_thread(self.engine.remove, helper)
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
            return record, None
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
            record["_drop"] = True
            return record, None
        audio = source_has_audio(source, self.data_dir)
        record["has_audio"] = audio
        src = with_source_bind(with_output(src, camera.output, audio=audio), camera.source_ip)
        record["producer"] = src
        return record, src

    async def _commit(self, record: dict, src: str | None) -> None:
        camera_id = record["camera_id"]
        if record.pop("_drop", False):
            if self._applied.get(camera_id):
                await asyncio.to_thread(self.engine.remove, camera_id)
                self._applied.pop(camera_id, None)
            self.runtime[camera_id] = record
            return
        if src is None:
            self.runtime[camera_id] = record
            return
        if self._applied.get(camera_id) != src:
            engine_error = await asyncio.to_thread(self.engine.upsert, camera_id, src)
            if engine_error:
                record["engine_error"] = engine_error
            else:
                self._applied[camera_id] = src
        self.runtime[camera_id] = record
