"""Virtual camera documents stored on disk."""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from ipaddress import IPv4Address

SOURCE_TYPES = ("ha_camera", "rtsp", "onvif", "image")
OPERATORS = ("eq", "ne", "gt", "lt")
RESOLUTIONS = ((1920, 1080), (1280, 720), (640, 480), (640, 360))
H264_PROFILES = ("Baseline", "Main", "High")
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_ENTITY = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def xml_escape(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@dataclass
class Source:
    id: str
    type: str
    name: str = ""
    entity_id: str = ""
    url: str = ""
    host: str = ""
    port: int = 80
    username: str = ""
    password: str = ""
    image_path: str = ""
    image_url: str = ""

    def kind(self) -> str:
        return "image" if self.type == "image" else "stream"


@dataclass
class Rule:
    id: str
    entity_id: str
    operator: str
    value: str
    source_id: str


@dataclass
class OutputSettings:
    """Encoder settings advertised on ONVIF and applied to the published RTSP stream."""

    width: int = 1920
    height: int = 1080
    framerate: int = 15
    bitrate_kbps: int = 2048
    h264_profile: str = "Main"
    gov_length: int = 30


@dataclass
class Camera:
    id: str
    name: str
    enabled: bool = True
    interface: str = ""
    listen_ip: str = "127.0.0.1"
    virtual_ip: bool = False
    service_port: int = 0
    prefix_length: int = 24
    source_interface: str = ""
    source_ip: str = ""
    username: str = ""
    password: str = ""
    rules_enabled: bool = False
    fallback_source_id: str = ""
    sources: list[Source] = field(default_factory=list)
    rules: list[Rule] = field(default_factory=list)
    endpoint_uuid: str = ""
    scopes_extra: list[str] = field(default_factory=list)
    discovery_mode: str = "Discoverable"
    output: OutputSettings = field(default_factory=OutputSettings)

    def source_map(self) -> dict[str, Source]:
        return {source.id: source for source in self.sources}


@dataclass
class Endpoints:
    device: str
    media: str
    stream: str
    snapshot: str


def endpoints_for(camera: Camera, onvif_port: int, rtsp_port: int) -> Endpoints:
    ip = camera.listen_ip
    if camera.virtual_ip:
        port = camera.service_port or 80
        host = ip if port == 80 else f"{ip}:{port}"
        base = f"http://{host}/onvif"
    else:
        base = f"http://{ip}:{onvif_port}/onvif/{camera.id}"
    return Endpoints(
        device=f"{base}/device_service",
        media=f"{base}/media_service",
        stream=f"rtsp://{ip}:{rtsp_port}/{camera.id}",
        snapshot=f"{base}/snapshot.jpg",
    )


def _token(value: object, fallback: str | None = None) -> str:
    text = str(value or "").strip()
    if _TOKEN.match(text):
        return text
    if fallback:
        return fallback
    return new_id()


def _require_text(payload: dict, key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def source_from_dict(raw: dict) -> Source:
    if not isinstance(raw, dict):
        raise ValueError("source must be an object")
    source_type = str(raw.get("type") or "").strip()
    if source_type not in SOURCE_TYPES:
        raise ValueError("source type must be ha_camera, rtsp, onvif, or image")
    source = Source(
        id=_token(raw.get("id")),
        type=source_type,
        name=str(raw.get("name") or "").strip(),
        entity_id=str(raw.get("entity_id") or "").strip(),
        url=str(raw.get("url") or "").strip(),
        host=str(raw.get("host") or "").strip(),
        port=int(raw.get("port") or 80),
        username=str(raw.get("username") or ""),
        password=str(raw.get("password") or ""),
        image_path=str(raw.get("image_path") or "").strip(),
        image_url=str(raw.get("image_url") or "").strip(),
    )
    if not source.name:
        source.name = source.type
    if not 1 <= source.port <= 65535:
        raise ValueError("ONVIF source port is out of range")
    if source.type == "ha_camera":
        if not _ENTITY.match(source.entity_id) or not source.entity_id.startswith("camera."):
            raise ValueError("Home Assistant camera source needs a camera entity id")
    elif source.type == "rtsp":
        if not (source.url.startswith("rtsp://") or source.url.startswith("rtsps://")):
            raise ValueError("RTSP source URL must start with rtsp:// or rtsps://")
    elif source.type == "onvif":
        if not source.host:
            raise ValueError("ONVIF source needs a host")
    elif source.type == "image":
        if not source.image_path and not source.image_url:
            raise ValueError("image source needs a file or an http(s) URL")
        if source.image_url and not (
            source.image_url.startswith("http://") or source.image_url.startswith("https://")
        ):
            raise ValueError("image URL must start with http:// or https://")
        if '"' in source.image_path or '"' in source.image_url:
            raise ValueError("image path cannot contain quotes")
        if source.image_path and (".." in Path(source.image_path).parts or Path(source.image_path).is_absolute()):
            raise ValueError("image_path must be a relative file inside the data directory")
    return source


def rule_from_dict(raw: dict) -> Rule:
    if not isinstance(raw, dict):
        raise ValueError("rule must be an object")
    operator = str(raw.get("operator") or "").strip()
    if operator not in OPERATORS:
        raise ValueError("rule operator must be eq, ne, gt, or lt")
    entity_id = str(raw.get("entity_id") or "").strip()
    if not _ENTITY.match(entity_id):
        raise ValueError("rule needs an entity id such as switch.example")
    return Rule(
        id=_token(raw.get("id")),
        entity_id=entity_id,
        operator=operator,
        value=str(raw.get("value") if raw.get("value") is not None else ""),
        source_id=_token(raw.get("source_id"), fallback="missing"),
    )


def _bounded_int(value: object, default: int, low: int, high: int, label: str) -> int:
    if value is None or value == "":
        number = default
    else:
        try:
            number = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a whole number") from exc
    if not low <= number <= high:
        raise ValueError(f"{label} must be between {low} and {high}")
    return number


def output_from_dict(raw: object) -> OutputSettings:
    if raw is None:
        return OutputSettings()
    if not isinstance(raw, dict):
        raise ValueError("output must be an object")
    width = _bounded_int(raw.get("width"), 1920, 160, 1920, "width")
    height = _bounded_int(raw.get("height"), 1080, 120, 1080, "height")
    if (width, height) not in RESOLUTIONS:
        choices = ", ".join(f"{item[0]}x{item[1]}" for item in RESOLUTIONS)
        raise ValueError(f"resolution must be one of {choices}")
    profile = str(raw.get("h264_profile") or "Main").strip()
    matched = next((item for item in H264_PROFILES if item.casefold() == profile.casefold()), "")
    if not matched:
        raise ValueError("H.264 profile must be Baseline, Main, or High")
    return OutputSettings(
        width=width,
        height=height,
        framerate=_bounded_int(raw.get("framerate"), 15, 1, 30, "frame rate"),
        bitrate_kbps=_bounded_int(raw.get("bitrate_kbps"), 2048, 128, 8192, "bitrate"),
        h264_profile=matched,
        gov_length=_bounded_int(raw.get("gov_length"), 30, 1, 120, "GOP length"),
    )


def camera_from_dict(raw: dict, existing: Camera | None = None) -> Camera:
    if not isinstance(raw, dict):
        raise ValueError("camera must be an object")
    name = _require_text(raw, "name")
    listen_ip = str(raw.get("listen_ip") or "").strip()
    if not listen_ip:
        listen_ip = "0.0.0.0"
    try:
        IPv4Address(listen_ip)
    except ValueError as exc:
        raise ValueError("broadcast address must be an IPv4 address") from exc
    source_ip = str(raw.get("source_ip") or "").strip()
    if source_ip:
        try:
            IPv4Address(source_ip)
        except ValueError as exc:
            raise ValueError("listening network must be an IPv4 address") from exc
    sources = [source_from_dict(item) for item in raw.get("sources") or []]
    if not sources:
        raise ValueError("add at least one source")
    seen: set[str] = set()
    for source in sources:
        if source.id in seen:
            raise ValueError("source ids must be unique")
        seen.add(source.id)
    rules = [rule_from_dict(item) for item in raw.get("rules") or []]
    for rule in rules:
        if rule.source_id not in seen:
            raise ValueError(f"rule {rule.entity_id} points at a missing source")
    fallback = str(raw.get("fallback_source_id") or "").strip()
    if fallback not in seen:
        raise ValueError("choose a fallback source")
    camera_id = _token(raw.get("id"), fallback=existing.id if existing else None)
    return Camera(
        id=camera_id,
        name=name,
        enabled=bool(raw.get("enabled", True)),
        interface=str(raw.get("interface") or "").strip(),
        listen_ip=listen_ip,
        virtual_ip=bool(raw.get("virtual_ip", False)),
        service_port=int(raw.get("service_port") or 0),
        prefix_length=int(raw.get("prefix_length") or 24),
        source_interface=str(raw.get("source_interface") or "").strip(),
        source_ip=source_ip,
        username=str(raw.get("username") or ""),
        password=str(raw.get("password") or ""),
        rules_enabled=bool(raw.get("rules_enabled", False)),
        fallback_source_id=fallback,
        sources=sources,
        rules=rules,
        endpoint_uuid=(existing.endpoint_uuid if existing and existing.endpoint_uuid else "")
        or str(raw.get("endpoint_uuid") or "")
        or str(uuid.uuid4()),
        scopes_extra=list(existing.scopes_extra) if existing else list(raw.get("scopes_extra") or []),
        discovery_mode=existing.discovery_mode if existing else str(raw.get("discovery_mode") or "Discoverable"),
        output=output_from_dict(raw.get("output")),
    )


FIXED_OUTPUT = (
    ("Profile token", "main"),
    ("Profile fixed", "Yes"),
    ("Encoding", "H264"),
    ("H.264 level", "4.1"),
    ("Pixel format", "yuv420p"),
    ("Audio", "None"),
    ("Transport", "RTP/RTSP/TCP"),
    ("Multicast", "Off"),
    ("Encoder", "libx264, superfast, zerolatency"),
    ("ONVIF quality", "5"),
)


def camera_to_dict(camera: Camera) -> dict:
    data = asdict(camera)
    data["fixed_output"] = [{"label": label, "value": value} for label, value in FIXED_OUTPUT]
    return data
