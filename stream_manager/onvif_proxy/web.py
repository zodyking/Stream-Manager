"""HTTP UI, JSON API, ONVIF SOAP, and the local HA media proxy."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import time
import uuid
from pathlib import Path
from urllib.parse import quote, unquote, urljoin

from aiohttp import web

from .aliases import is_wireless, place_camera
from .interfaces import list_interfaces
from .models import camera_from_dict, camera_to_dict, endpoints_for
from .onvif_soap import dispatch

log = logging.getLogger("onvif_proxy.web")

STATIC = Path(__file__).resolve().parent.parent / "static"
_INGRESS = re.compile(r"^/[A-Za-z0-9_./-]*$")
_EXTRA_SLASHES = re.compile(r"/{2,}")
_RESTORE_EVERY = 60.0


@web.middleware
async def collapse_slashes(request: web.Request, handler):
    """Ingress joins its prefix with another slash, so the panel arrives as ///."""
    path = request.rel_url.path
    if "//" not in path:
        return await handler(request)
    cleaned = _EXTRA_SLASHES.sub("/", path) or "/"
    cloned = request.clone(rel_url=request.rel_url.with_path(cleaned))
    match_info = await request.app.router.resolve(cloned)
    match_info.add_app(request.app)
    match_info.freeze()
    cloned._match_info = match_info
    return await match_info.handler(cloned)


def _ingress_prefix(request: web.Request) -> str:
    raw = request.headers.get("X-Ingress-Path", "")
    if raw and _INGRESS.match(raw):
        return raw.rstrip("/")
    return ""


def _json(data: object, status: int = 200) -> web.Response:
    return web.json_response(data, status=status)


def _error(message: str, status: int = 400) -> web.Response:
    return _json({"error": message}, status=status)


def _local(request: web.Request) -> bool:
    peer = request.remote or ""
    return peer in {"127.0.0.1", "::1"}


async def index(request: web.Request) -> web.Response:
    prefix = _ingress_prefix(request)
    root = f"{prefix}/" if prefix else ""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = html.replace("__INGRESS__", prefix).replace("__PREFIX__/", root)
    return web.Response(text=html, content_type="text/html")


_STATIC_TYPES = {
    ".css": "text/css",
    ".js": "text/javascript",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


async def static_file(request: web.Request) -> web.Response:
    raw = request.match_info["name"]
    parts = Path(raw).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise web.HTTPNotFound()
    path = (STATIC / raw).resolve()
    try:
        path.relative_to(STATIC.resolve())
    except ValueError:
        raise web.HTTPNotFound() from None
    content_type = _STATIC_TYPES.get(path.suffix.lower())
    if content_type is None or not path.is_file():
        raise web.HTTPNotFound()
    if path.suffix.lower() in {".png", ".ico"}:
        return web.Response(body=path.read_bytes(), content_type=content_type)
    return web.Response(text=path.read_text(encoding="utf-8"), content_type=content_type)


def _public(app: web.Application, camera) -> dict:
    data = camera_to_dict(camera)
    runtime = app["streams"].runtime.get(camera.id, {})
    data["runtime"] = runtime
    urls = endpoints_for(camera, app["settings"].onvif_port, app["settings"].rtsp_port)
    data["urls"] = {"rtsp": urls.stream, "onvif": urls.device, "media": urls.media, "snapshot": urls.snapshot}
    return data


def _place(app: web.Application, camera, previous) -> None:
    place_camera(camera, previous, list_interfaces(), app["aliases"])


def migrate_addresses(app: web.Application) -> None:
    """Move cameras saved before per-camera MACs onto their own macvlan, keeping the IP."""
    changed = False
    for camera in app["store"].cameras:
        if not camera.virtual_ip or camera.link or not camera.interface or is_wireless(camera.interface):
            continue
        previous = copy.deepcopy(camera)
        try:
            _place(app, camera, previous)
        except ValueError as exc:
            log.warning("could not give %s its own MAC: %s", camera.name, exc)
            continue
        log.info("%s now answers on %s with MAC %s", camera.name, camera.listen_ip, camera.mac)
        changed = True
    if changed:
        app["store"].save()


async def sync_alias_sites(app: web.Application) -> None:
    runner = app.get("runner")
    if runner is None or runner.server is None:
        return
    sites: dict = app.setdefault("alias_sites", {})
    wanted = {
        camera.listen_ip: camera
        for camera in app["store"].cameras
        if camera.enabled and camera.virtual_ip and (camera.service_port or 80) == 80
    }
    for ip in list(sites):
        if ip not in wanted:
            await sites.pop(ip).stop()
    changed = False
    for ip, camera in wanted.items():
        if ip in sites:
            continue
        site = web.TCPSite(runner, ip, 80)
        try:
            await site.start()
        except (OSError, RuntimeError) as exc:
            log.warning("ONVIF port 80 on %s is unavailable: %s", ip, exc)
            camera.service_port = app["settings"].onvif_port
            changed = True
            continue
        sites[ip] = site
    if changed:
        app["store"].save()


async def _resync(app: web.Application) -> None:
    now = time.monotonic()
    if now - app.get("aliases_restored", -_RESTORE_EVERY) >= _RESTORE_EVERY:
        app["aliases_restored"] = now
        await asyncio.to_thread(app["aliases"].restore)
    await sync_alias_sites(app)
    discovery_ads = []
    for camera in app["store"].cameras:
        if not camera.enabled:
            continue
        urls = endpoints_for(camera, app["settings"].onvif_port, app["settings"].rtsp_port)
        from .discovery import scope_list

        discovery_ads.append(
            {
                "ip": camera.listen_ip,
                "endpoint_uuid": camera.endpoint_uuid,
                "xaddr": urls.device,
                "scopes": scope_list(camera.name) + list(camera.scopes_extra),
                "discovery_mode": camera.discovery_mode,
            }
        )
    app["discovery"].update(discovery_ads)
    await app["streams"].sync(app["store"].cameras, dict(app["ha"].states), app["ha"])


async def status(request: web.Request) -> web.Response:
    app = request.app
    settings = app["settings"]
    engine = app["engine"]
    return _json(
        {
            "version": "1.0.0",
            "ha": {
                "connected": app["ha"].connected or app["ha"].ws_connected,
                "websocket": app["ha"].ws_connected,
                "mode": app["ha"].mode,
                "error": app["ha"].error,
                "url": "" if settings.supervisor else settings.ha_url,
            },
            "discovery": app["discovery"].status,
            "arp_isolation": app["aliases"].arp_mode,
            "engine": {
                "running": bool(engine.running and engine.ping()) if engine.binary else False,
                "binary": bool(engine.binary),
                "error": engine.error,
            },
            "cameras": len(app["store"].cameras),
            "dev": settings.dev,
            "ports": {"ui": settings.ui_port, "onvif": settings.onvif_port, "rtsp": settings.rtsp_port},
        }
    )


async def interfaces(_request: web.Request) -> web.Response:
    return _json({"interfaces": list_interfaces()})


async def list_cameras(request: web.Request) -> web.Response:
    return _json({"cameras": [_public(request.app, camera) for camera in request.app["store"].cameras]})


async def get_camera(request: web.Request) -> web.Response:
    camera = request.app["store"].get(request.match_info["camera_id"])
    if camera is None:
        return _error("camera not found", 404)
    return _json(_public(request.app, camera))


async def _read_camera(request: web.Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        raise web.HTTPBadRequest(text="invalid JSON")
    return payload


async def create_camera(request: web.Request) -> web.Response:
    payload = await _read_camera(request)
    payload["id"] = ""
    try:
        camera = camera_from_dict(payload)
        await asyncio.to_thread(_place, request.app, camera, None)
    except ValueError as exc:
        return _error(str(exc))
    request.app["store"].upsert(camera)
    await _resync(request.app)
    return _json(_public(request.app, camera), status=201)


async def update_camera(request: web.Request) -> web.Response:
    current = request.app["store"].get(request.match_info["camera_id"])
    if current is None:
        return _error("camera not found", 404)
    payload = await _read_camera(request)
    payload["id"] = current.id
    try:
        camera = camera_from_dict(payload, existing=current)
        await asyncio.to_thread(_place, request.app, camera, current)
    except ValueError as exc:
        return _error(str(exc))
    request.app["store"].upsert(camera)
    await _resync(request.app)
    return _json(_public(request.app, camera))


async def delete_camera(request: web.Request) -> web.Response:
    camera_id = request.match_info["camera_id"]
    current = request.app["store"].get(camera_id)
    if current and current.virtual_ip:
        await asyncio.to_thread(request.app["aliases"].release, current.listen_ip)
    if not request.app["store"].delete(camera_id):
        return _error("camera not found", 404)
    await _resync(request.app)
    return _json({"deleted": camera_id})


async def entities(request: web.Request) -> web.Response:
    ha = request.app["ha"]
    rows = list(ha.entities.values())
    rows.sort(key=lambda item: item["entity_id"])
    return _json({"connected": ha.connected or ha.ws_connected, "error": ha.error, "entities": rows})


async def dev_states(request: web.Request) -> web.Response:
    if not request.app["settings"].dev:
        return _error("dev state injection is disabled", 404)
    payload = await request.json()
    if not isinstance(payload, dict):
        return _error("expected an object of entity ids to states")
    request.app["ha"].inject_states({str(key): str(value) for key, value in payload.items()})
    await _resync(request.app)
    return _json({"states": request.app["ha"].states})


async def upload_image(request: web.Request) -> web.Response:
    reader = await request.multipart()
    field = await reader.next()
    if field is None:
        return _error("upload a file field")
    suffix = Path(field.filename or "image.jpg").suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".jpg"
    name = uuid.uuid4().hex + suffix
    folder = request.app["settings"].data_dir / "images"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    size = 0
    with target.open("wb") as handle:
        while True:
            chunk = await field.read_chunk(65536)
            if not chunk:
                break
            size += len(chunk)
            if size > 8_000_000:
                target.unlink(missing_ok=True)
                return _error("image is larger than 8 MB")
            handle.write(chunk)
    return _json({"image_path": f"images/{name}"})


def _camera_for_request(request: web.Request):
    camera_id = request.match_info.get("camera_id")
    if camera_id:
        return request.app["store"].get(camera_id)
    sockname = request.transport.get_extra_info("sockname") if request.transport else None
    ip = sockname[0] if sockname else ""
    if ip in {"", "0.0.0.0"}:
        ip = request.headers.get("Host", "").split(":")[0]
    for camera in request.app["store"].cameras:
        if camera.enabled and camera.virtual_ip and camera.listen_ip == ip:
            return camera
    return None


async def soap(request: web.Request) -> web.Response:
    camera = _camera_for_request(request)
    if camera is None or not camera.enabled:
        return web.Response(status=404, text="camera not found")
    xml = await request.text()
    endpoints = endpoints_for(camera, request.app["settings"].onvif_port, request.app["settings"].rtsp_port)
    result = dispatch(
        xml,
        camera,
        endpoints,
        method=request.method,
        authorization=request.headers.get("Authorization", ""),
        nonce=request.app["digest_nonce"],
    )
    if result.status == 200:
        request.app["store"].save()
    headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
    if result.www_authenticate:
        headers["WWW-Authenticate"] = result.www_authenticate
    return web.Response(status=result.status, text=result.body, headers=headers)


async def snapshot(request: web.Request) -> web.Response:
    app = request.app
    camera = _camera_for_request(request)
    if camera is None or not camera.enabled:
        return web.Response(status=404, text="camera not found")
    runtime = app["streams"].runtime.get(camera.id) or {}
    source_id = runtime.get("source_id")
    source = next((item for item in camera.sources if item.id == source_id), None)
    if source and source.type == "image" and source.image_path:
        path = Path(source.image_path)
        if not path.is_absolute():
            path = app["settings"].data_dir / source.image_path
        if path.is_file():
            return web.FileResponse(path)
    if source and source.type == "image" and source.image_url:
        return await _proxy_bytes(app, source.image_url, {})
    if source and source.type == "ha_camera":
        url = app["ha"].api_url(f"/api/camera_proxy/{source.entity_id}")
        return await _proxy_bytes(app, url, app["ha"].auth_headers())
    if app["engine"].binary:
        frame = f"http://127.0.0.1:{app['settings'].go2rtc_api_port}/api/frame.jpeg?src={quote(camera.id)}"
        response = await _proxy_bytes(app, frame, {})
        if response.status == 200 and not response.body:
            return web.Response(status=503, text="the stream did not produce a frame")
        return response
    return web.Response(status=503, text="snapshot is not available for the active source")


def _peer(item: dict) -> dict:
    tracks = [track for track in (item.get("receivers") or []) + (item.get("senders") or []) if isinstance(track, dict)]
    total = int(item.get("bytes_recv") or item.get("bytes_send") or 0) or sum(int(track.get("bytes") or 0) for track in tracks)
    codecs = sorted({str((track.get("codec") or {}).get("codec_name") or "") for track in tracks} - {""})
    return {
        "remote": str(item.get("remote_addr") or "").split(" ")[0],
        "agent": str(item.get("user_agent") or ""),
        "kind": str(item.get("format_name") or item.get("protocol") or ""),
        "bytes": total,
        "codecs": codecs,
    }


async def stream_check(request: web.Request) -> web.Response:
    """What go2rtc currently sees for this camera, without starting the source."""
    app = request.app
    camera = app["store"].get(request.match_info["camera_id"])
    if camera is None:
        return _error("camera not found", 404)
    engine = app["engine"]
    lines = [line for line in engine.recent if camera.id in line or "exec" in line or "ffmpeg" in line]
    payload = {"engine": bool(engine.running), "producers": [], "consumers": [], "log": lines[-12:]}
    session = app["ha"]._session
    if not engine.binary or session is None:
        return _json(payload)
    url = f"http://127.0.0.1:{app['settings'].go2rtc_api_port}/api/streams?src={quote(camera.id)}"
    try:
        async with session.get(url) as response:
            info = await response.json(content_type=None) if response.status == 200 else {}
    except Exception as exc:
        payload["error"] = str(exc)
        return _json(payload)
    info = info if isinstance(info, dict) else {}
    payload["producers"] = [_peer(item) for item in info.get("producers") or [] if isinstance(item, dict)]
    payload["consumers"] = [_peer(item) for item in info.get("consumers") or [] if isinstance(item, dict)]
    return _json(payload)


async def _proxy_bytes(app: web.Application, url: str, headers: dict) -> web.Response:
    session = app["ha"]._session
    if session is None:
        return web.Response(status=503, text="HTTP client is not ready")
    try:
        async with session.get(url, headers=headers) as response:
            body = await response.read()
            content_type = response.headers.get("Content-Type", "application/octet-stream")
            return web.Response(status=response.status, body=body, content_type=content_type)
    except Exception as exc:
        return web.Response(status=502, text=str(exc))


def _rewrite_playlist(text: str, playlist_url: str, entity_id: str, prefix: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            if 'URI="' in line:
                def replacer(match: re.Match) -> str:
                    absolute = urljoin(playlist_url, match.group(1))
                    proxied = f"{prefix}/internal/ha/{entity_id}/fetch?url={quote(absolute, safe='')}"
                    return f'URI="{proxied}"'

                line = re.sub(r'URI="([^"]+)"', replacer, line)
            lines.append(line)
            continue
        absolute = urljoin(playlist_url, stripped)
        lines.append(f"{prefix}/internal/ha/{entity_id}/fetch?url={quote(absolute, safe='')}")
    return "\n".join(lines) + "\n"


async def hls_playlist(request: web.Request) -> web.Response:
    if not _local(request):
        return web.Response(status=403, text="local only")
    entity_id = request.match_info["entity_id"]
    ha = request.app["ha"]
    try:
        upstream = await ha.ensure_hls(entity_id)
    except Exception as exc:
        return web.Response(status=502, text=str(exc))
    if not ha.allowed_upstream(upstream):
        return web.Response(status=403, text="refusing upstream outside Home Assistant")
    session = ha._session
    assert session is not None
    async with session.get(upstream, headers=ha.auth_headers()) as response:
        text = await response.text()
        if response.status != 200:
            return web.Response(status=response.status, text=text)
    prefix = ""
    rewritten = _rewrite_playlist(text, upstream, entity_id, prefix)
    return web.Response(text=rewritten, content_type="application/vnd.apple.mpegurl")


async def hls_fetch(request: web.Request) -> web.Response:
    if not _local(request):
        return web.Response(status=403, text="local only")
    ha = request.app["ha"]
    target = unquote(request.query.get("url", ""))
    if not ha.allowed_upstream(target):
        return web.Response(status=403, text="refusing upstream outside Home Assistant")
    session = ha._session
    assert session is not None
    async with session.get(target, headers=ha.auth_headers()) as response:
        content_type = response.headers.get("Content-Type", "")
        if "mpegurl" in content_type or target.endswith(".m3u8"):
            text = await response.text()
            entity_id = request.match_info["entity_id"]
            rewritten = _rewrite_playlist(text, target, entity_id, "")
            return web.Response(status=response.status, text=rewritten, content_type="application/vnd.apple.mpegurl")
        body = await response.read()
        return web.Response(status=response.status, body=body, content_type=content_type or "application/octet-stream")


async def mjpeg(request: web.Request) -> web.Response:
    if not _local(request):
        return web.Response(status=403, text="local only")
    ha = request.app["ha"]
    entity_id = request.match_info["entity_id"]
    url = ha.api_url(f"/api/camera_proxy_stream/{entity_id}")
    session = ha._session
    assert session is not None
    upstream = await session.get(url, headers=ha.auth_headers())
    response = web.StreamResponse(status=upstream.status, headers={"Content-Type": upstream.headers.get("Content-Type", "multipart/x-mixed-replace")})
    await response.prepare(request)
    try:
        async for chunk in upstream.content.iter_chunked(65536):
            await response.write(chunk)
    finally:
        upstream.release()
    return response


def setup_routes(app: web.Application) -> None:
    app.middlewares.append(collapse_slashes)
    app.router.add_get("/", index)
    app.router.add_get("/static/{name:.*}", static_file)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/interfaces", interfaces)
    app.router.add_get("/api/cameras", list_cameras)
    app.router.add_post("/api/cameras", create_camera)
    app.router.add_get("/api/cameras/{camera_id}", get_camera)
    app.router.add_put("/api/cameras/{camera_id}", update_camera)
    app.router.add_delete("/api/cameras/{camera_id}", delete_camera)
    app.router.add_get("/api/cameras/{camera_id}/stream", stream_check)
    app.router.add_get("/api/entities", entities)
    app.router.add_post("/api/images", upload_image)
    app.router.add_post("/api/dev/states", dev_states)
    app.router.add_post("/onvif/device_service", soap)
    app.router.add_post("/onvif/media_service", soap)
    app.router.add_get("/onvif/snapshot.jpg", snapshot)
    app.router.add_post("/onvif/{camera_id}/device_service", soap)
    app.router.add_post("/onvif/{camera_id}/media_service", soap)
    app.router.add_get("/onvif/{camera_id}/snapshot.jpg", snapshot)
    app.router.add_get("/internal/ha/{entity_id}/playlist.m3u8", hls_playlist)
    app.router.add_get("/internal/ha/{entity_id}/fetch", hls_fetch)
    app.router.add_get("/internal/ha/{entity_id}/mjpeg", mjpeg)
