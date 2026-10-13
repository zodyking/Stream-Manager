"""Compose a designed picture into one H.264 stream that stays published.

Each source tile is a localhost MPEG-TS feed. When that writer stops, a slate
takes the same port. The published ffmpeg command does not change, so the
ONVIF stream UniFi Protect is watching is not rebuilt.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .models import Camera, Layer, OutputSettings, Source

log = logging.getLogger("onvif_proxy.compositor")

INK = "0x071320"
FOG = "0xECF8FD"
SLATE_TEXT = "No source"


def even(value: int) -> int:
    number = max(2, int(value))
    return number if number % 2 == 0 else number - 1


def slot_port(camera_id: str, layer_id: str, taken: set[int] | None = None) -> int:
    """Stable UDP port for a tile. The published command depends on this port."""
    digest = hashlib.sha1(f"{camera_id}:{layer_id}".encode()).digest()
    port = 40000 + int.from_bytes(digest[:2], "big") % 20000
    used = taken or set()
    while port in used:
        port = 40000 + (port - 40000 + 1) % 20000
    return port


def font_file() -> str:
    configured = os.environ.get("HASP_FONT", "")
    candidates = [
        configured,
        "/usr/share/fonts/ttf-dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/dejavu/DejaVuSans.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate))
    return candidates[1]


def filter_path(path: str) -> str:
    return path.replace("\\", "/").replace(":", "\\:")


def escape_drawtext(text: str) -> str:
    cleaned = text.replace("\n", " ").replace('"', "'").replace("{", "(").replace("}", ")")
    return (
        cleaned.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace("%", "\\%")
    )


def ffmpeg_color(value: str) -> str:
    text = (value or "").strip()
    if text.startswith("#") and len(text) == 7:
        return "0x" + text[1:].upper()
    return FOG


def canvas_video_args(output: OutputSettings) -> str:
    profile = output.h264_profile.casefold()
    rate = output.bitrate_kbps
    return (
        "-c:v libx264"
        f" -g {output.gov_length}"
        f" -r {output.framerate}"
        f" -profile:v {profile}"
        " -level:v 4.1 -preset:v superfast -tune:v zerolatency -pix_fmt:v yuv420p"
        " -bf 0 -x264-params sliced-threads=0:slices=1:repeat-headers=1:scenecut=0"
        f" -b:v {rate}k -maxrate {rate}k -bufsize {rate}k"
    )


def _pixels(layer: Layer, output: OutputSettings) -> tuple[int, int, int, int]:
    width = even(layer.w * output.width)
    height = even(layer.h * output.height)
    x = min(int(layer.x * output.width), max(0, output.width - width))
    y = min(int(layer.y * output.height), max(0, output.height - height))
    return x, y, width, height


def tile_size(layer: Layer, output: OutputSettings) -> tuple[int, int]:
    _x, _y, width, height = _pixels(layer, output)
    return width, height


def slate_command(width: int, height: int, fps: int, port: int, ffmpeg: str = "ffmpeg") -> list[str]:
    size = max(18, min(72, height // 8))
    draw = (
        f"drawtext=fontfile={filter_path(font_file())}:text='{SLATE_TEXT}':fontsize={size}:"
        f"fontcolor={FOG}:x=(w-text_w)/2:y=(h-text_h)/2"
    )
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"color=c={INK}:s={width}x{height}:r={fps}",
        "-vf",
        draw,
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(max(1, fps * 2)),
        "-bf",
        "0",
        "-f",
        "mpegts",
        f"udp://127.0.0.1:{port}?pkt_size=1316",
    ]


def _text_xy(layer: Layer, output: OutputSettings) -> tuple[str, str]:
    x, y, width, height = _pixels(layer, output)
    if layer.align == "left":
        xpos = str(x)
    elif layer.align == "right":
        xpos = f"{x + width}-text_w"
    else:
        xpos = f"{x}+({width}-text_w)/2"
    ypos = f"{y}+({height}-text_h)/2"
    return xpos, ypos


def _filter_steps(camera: Camera) -> list[str]:
    """Filter bodies in paint order. `{src}` and `{dst}` are filled by the caller."""
    output = camera.output
    ordered = sorted(camera.layers, key=lambda layer: (layer.z, layer.id))
    steps: list[str] = []
    source_input = 1
    font = filter_path(font_file())
    for layer in ordered:
        if layer.type == "box":
            x, y, width, height = _pixels(layer, output)
            steps.append(
                "[{src}]drawbox="
                f"x={x}:y={y}:w={width}:h={height}:color={ffmpeg_color(layer.fill)}@{layer.opacity:.2f}:t=fill"
                "[{dst}]"
            )
            if layer.border:
                steps.append(
                    "[{src}]drawbox="
                    f"x={x}:y={y}:w={width}:h={height}:color={ffmpeg_color(layer.border)}@1:t=4"
                    "[{dst}]"
                )
        elif layer.type == "text":
            xpos, ypos = _text_xy(layer, output)
            steps.append(
                "[{src}]drawtext="
                f"fontfile={font}:text='{escape_drawtext(layer.text or ' ')}':fontsize={layer.font_size}:"
                f"fontcolor={ffmpeg_color(layer.color)}:x={xpos}:y={ypos}"
                "[{dst}]"
            )
        else:
            x, y, width, height = _pixels(layer, output)
            steps.append(
                f"[{source_input}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={INK},fps={output.framerate},setpts=PTS-STARTPTS[s{source_input}]"
            )
            steps.append(
                f"[{{src}}][s{source_input}]overlay=x={x}:y={y}:eof_action=pass:repeatlast=1:shortest=0[{{dst}}]"
            )
            source_input += 1
    return steps


def compositor_input(camera: Camera) -> str:
    """ffmpeg arguments up to the mapped canvas. No source addresses."""
    output = camera.output
    ordered = sorted(camera.layers, key=lambda layer: (layer.z, layer.id))
    sources = [layer for layer in ordered if layer.type == "source"]
    taken: set[int] = set()
    ports: list[int] = []
    for layer in sources:
        port = slot_port(camera.id, layer.id, taken)
        taken.add(port)
        ports.append(port)
    parts = [f"-f lavfi -i color=c={INK}:s={output.width}x{output.height}:r={output.framerate}"]
    for port in ports:
        parts.append(
            "-thread_queue_size 64 -fflags nobuffer -flags low_delay -probesize 32768 -analyzeduration 0 "
            f"-f mpegts -i udp://127.0.0.1:{port}?fifo_size=1000000&overrun_nonfatal=1"
        )
    steps = _filter_steps(camera)
    graph = ["[0:v]format=yuv420p[b0]"]
    label = "b0"
    if not steps:
        graph = ["[0:v]format=yuv420p[vout]"]
    for index, step in enumerate(steps):
        dst = "vout" if index == len(steps) - 1 else f"b{index + 1}"
        if "{src}" not in step:
            graph.append(step)
            continue
        graph.append(step.replace("{src}", label).replace("{dst}", dst))
        label = dst
    return " ".join(parts) + ' -filter_complex "' + ";".join(graph) + '" -map [vout]'


def choose_writer(prefer_live: bool, live: list[str] | None, slate: list[str]) -> tuple[str, list[str]]:
    if prefer_live and live:
        return "live", live
    return "slate", slate


def live_input_args(
    source: Source,
    *,
    data_dir: Path,
    ui_port: int,
    ha_mode: str | None,
    source_ip: str,
    helper_url: str = "",
) -> list[str] | None:
    """ffmpeg input flags for a tile. None when this source cannot be opened yet."""
    if source.type == "rtsp" and source.url.startswith(("rtsp://", "rtsps://")):
        flags = ["-fflags", "nobuffer", "-flags", "low_delay", "-rtsp_transport", "tcp"]
        if source_ip:
            flags.extend(["-localaddr", source_ip])
        return flags + ["-i", source.url]
    if source.type == "image":
        target = ""
        if source.image_path:
            path = Path(source.image_path)
            if not path.is_absolute():
                path = data_dir / source.image_path
            target = str(path)
        elif source.image_url:
            target = source.image_url
        if not target:
            return None
        return ["-loop", "1", "-framerate", "5", "-i", target]
    if source.type == "media":
        path = Path(source.image_path)
        if not path.is_absolute():
            path = data_dir / source.image_path
        if not path.is_file():
            return None
        return ["-stream_loop", "-1", "-re", "-fflags", "+genpts", "-i", str(path)]
    if source.type == "ha_camera" and ha_mode in {"mjpeg", "hls"}:
        suffix = "mjpeg" if ha_mode == "mjpeg" else "playlist.m3u8"
        return ["-i", f"http://127.0.0.1:{ui_port}/internal/ha/{source.entity_id}/{suffix}"]
    if source.type == "onvif" and helper_url:
        return ["-rtsp_transport", "tcp", "-i", helper_url]
    return None


def live_command(
    source: Source,
    *,
    data_dir: Path,
    ui_port: int,
    ha_mode: str | None,
    source_ip: str,
    width: int,
    height: int,
    fps: int,
    port: int,
    helper_url: str = "",
    ffmpeg: str = "ffmpeg",
) -> list[str] | None:
    incoming = live_input_args(
        source,
        data_dir=data_dir,
        ui_port=ui_port,
        ha_mode=ha_mode,
        source_ip=source_ip,
        helper_url=helper_url,
    )
    if not incoming:
        return None
    return [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        *incoming,
        "-an",
        "-vf",
        (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={INK},fps={fps}"
        ),
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-tune",
        "zerolatency",
        "-pix_fmt",
        "yuv420p",
        "-g",
        str(max(1, fps * 2)),
        "-bf",
        "0",
        "-f",
        "mpegts",
        f"udp://127.0.0.1:{port}?pkt_size=1316",
    ]


def helper_stream_name(camera_id: str, layer_id: str) -> str:
    return "cv" + hashlib.sha1(f"{camera_id}:{layer_id}".encode()).hexdigest()[:12]


class SlotKeeper:
    """Keep one UDP port sending frames, switching to the slate when the source writer exits."""

    def __init__(self, spawn=None) -> None:
        self._spawn = spawn or _popen
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.live_args: list[str] | None = None
        self.slate_args: list[str] = []
        self.mode = "slate"

    def update(self, live_args: list[str] | None, slate_args: list[str]) -> None:
        with self._lock:
            changed = live_args != self.live_args or slate_args != self.slate_args
            self.live_args = live_args
            self.slate_args = list(slate_args)
            start = self._thread is None
        if os.environ.get("HASP_CANVAS_WRITERS") == "0":
            return
        if start:
            self._thread = threading.Thread(target=self._loop, name="canvas-slot", daemon=True)
            self._thread.start()
        elif changed:
            self._wake.set()
            self._kill()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._kill()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def _kill(self) -> None:
        with self._lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _loop(self) -> None:
        prefer_live = True
        while not self._stop.is_set():
            with self._lock:
                live = self.live_args
                slate = list(self.slate_args)
            mode, args = choose_writer(prefer_live, live, slate)
            if not args:
                self._stop.wait(0.5)
                continue
            self.mode = mode
            if mode == "slate":
                log.info("tile on port %s is showing No source", _port_of(args))
            proc = self._spawn(args)
            with self._lock:
                self._proc = proc
            if mode == "live":
                proc.wait()
                prefer_live = False
            else:
                self._wake.wait(3)
                self._wake.clear()
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                else:
                    time.sleep(0.5)
                prefer_live = True
            if self._stop.is_set():
                self._kill()
                break


def _port_of(args: list[str]) -> str:
    for arg in args:
        if arg.startswith("udp://"):
            return arg
    return ""


def _popen(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class CanvasBoard:
    """Slot writers for every designed camera. The published encoder is not one of them."""

    def __init__(self, data_dir: Path, ui_port: int, rtsp_port: int) -> None:
        self.data_dir = data_dir
        self.ui_port = ui_port
        self.rtsp_port = rtsp_port
        self._keepers: dict[tuple[str, str], SlotKeeper] = {}
        self._helpers: dict[str, set[str]] = {}
        self._lock = threading.Lock()

    def modes(self, camera_id: str) -> dict[str, str]:
        with self._lock:
            return {layer_id: keeper.mode for (cam, layer_id), keeper in self._keepers.items() if cam == camera_id}

    def drop(self, camera_id: str) -> list[str]:
        """Stop tile writers and return helper stream names the engine should remove."""
        with self._lock:
            keys = [key for key in self._keepers if key[0] == camera_id]
            keepers = [self._keepers.pop(key) for key in keys]
            helpers = self._helpers.pop(camera_id, set())
        for keeper in keepers:
            keeper.stop()
        return sorted(helpers)

    def drop_all(self) -> list[str]:
        with self._lock:
            camera_ids = {key[0] for key in self._keepers} | set(self._helpers)
        names: list[str] = []
        for camera_id in camera_ids:
            names.extend(self.drop(camera_id))
        return names

    def sync_camera(
        self,
        camera: Camera,
        *,
        ha_modes: dict[str, str | None],
        helpers: dict[str, str],
    ) -> None:
        """helpers maps layer id to an RTSP url go2rtc is already publishing for ONVIF sources."""
        ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
        output = camera.output
        wanted = {layer.id for layer in camera.layers if layer.type == "source"}
        with self._lock:
            stale = [key for key in self._keepers if key[0] == camera.id and key[1] not in wanted]
            for key in stale:
                self._keepers.pop(key).stop()
        taken: set[int] = set()
        for layer in camera.layers:
            if layer.type != "source":
                continue
            port = slot_port(camera.id, layer.id, taken)
            taken.add(port)
            width, height = tile_size(layer, output)
            slate = slate_command(width, height, output.framerate, port, ffmpeg)
            source = camera.source_map().get(layer.source_id)
            live = None
            if source is not None:
                live = live_command(
                    source,
                    data_dir=self.data_dir,
                    ui_port=self.ui_port,
                    ha_mode=ha_modes.get(layer.source_id),
                    source_ip=camera.source_ip,
                    width=width,
                    height=height,
                    fps=output.framerate,
                    port=port,
                    helper_url=helpers.get(layer.id, ""),
                    ffmpeg=ffmpeg,
                )
            with self._lock:
                keeper = self._keepers.get((camera.id, layer.id))
                if keeper is None:
                    keeper = SlotKeeper()
                    self._keepers[(camera.id, layer.id)] = keeper
            keeper.update(live, slate)

    def note_helpers(self, camera_id: str, names: set[str]) -> list[str]:
        with self._lock:
            previous = self._helpers.get(camera_id, set())
            self._helpers[camera_id] = set(names)
        return sorted(previous - names)
