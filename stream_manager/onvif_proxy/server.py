"""Compose the add-on process."""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid

from aiohttp import web

from .aliases import AddressBook
from .config import Settings, Store, load_settings, pick_port
from .discovery import DiscoveryService
from .engine import Go2RtcEngine, find_binary
from .ha_client import HaClient
from .stream_manager import StreamManager
from .web import _resync, migrate_addresses, setup_routes, sync_alias_sites

log = logging.getLogger("onvif_proxy")


def build_app(settings: Settings | None = None) -> web.Application:
    settings = settings or load_settings()
    app = web.Application(client_max_size=70 * 1024 * 1024)
    binary = find_binary(settings.go2rtc_bin)
    if binary:
        settings.rtsp_port_requested = settings.rtsp_port
        settings.rtsp_port = pick_port(settings.rtsp_port, (8564, 8574, 18554, 28554))
        if settings.rtsp_port != settings.rtsp_port_requested:
            log.warning(
                "RTSP port %s is used by another program; publishing streams on %s instead",
                settings.rtsp_port_requested,
                settings.rtsp_port,
            )
        api_port = pick_port(settings.go2rtc_api_port, (19851, 19861, 29841), host="127.0.0.1")
        if api_port != settings.go2rtc_api_port:
            log.warning("go2rtc API port %s is busy; using %s", settings.go2rtc_api_port, api_port)
            settings.go2rtc_api_port = api_port
    engine = Go2RtcEngine(binary, settings.go2rtc_api_port, settings.rtsp_port, settings.data_dir)
    ha = HaClient(settings.ha_url, settings.ha_token, settings.supervisor, settings.poll_seconds)
    streams = StreamManager(engine, settings.ui_port, settings.onvif_port, settings.rtsp_port, settings.data_dir)
    app["settings"] = settings
    app["store"] = Store(settings.data_dir / "cameras.json")
    app["engine"] = engine
    app["ha"] = ha
    app["streams"] = streams
    app["discovery"] = DiscoveryService()
    app["aliases"] = AddressBook(settings.data_dir / "aliases.json")
    app["alias_sites"] = {}
    app["digest_nonce"] = uuid.uuid4().hex
    setup_routes(app)

    async def on_startup(_app: web.Application) -> None:
        engine.start()
        if engine.binary:
            for _ in range(20):
                if engine.ping():
                    engine.running = True
                    engine.error = ""
                    break
                await asyncio.sleep(0.25)
            else:
                if engine.process and engine.process.poll() is not None:
                    engine.running = False
                    engine.error = engine.error or "go2rtc did not stay running"
        await ha.start()

        def kick() -> None:
            asyncio.create_task(_resync(_app))

        ha.on_change = kick
        await asyncio.to_thread(migrate_addresses, _app)
        await _resync(_app)

        async def cycle_loop() -> None:
            """Rotate timed sources even when Home Assistant is not connected."""
            while True:
                await asyncio.sleep(1)
                try:
                    cameras = _app["store"].cameras
                    if not any(camera.enabled and camera.cycle_enabled and len(camera.sources) > 1 for camera in cameras):
                        continue
                    await _app["streams"].sync(cameras, dict(_app["ha"].states), _app["ha"])
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("timed source switch failed")

        _app["cycle_task"] = asyncio.create_task(cycle_loop())
        log.info("UI http://127.0.0.1:%s", settings.ui_port)
        log.info("ONVIF http://127.0.0.1:%s/onvif/<camera>/device_service", settings.onvif_port)

    async def on_cleanup(_app: web.Application) -> None:
        task = _app.get("cycle_task")
        if task is not None:
            task.cancel()
        _app["discovery"].stop()
        _app["aliases"].teardown()
        await ha.stop()
        engine.stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


async def serve(settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    app = build_app(settings)
    runner = web.AppRunner(app)
    app["runner"] = runner
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.ui_port).start()
    if settings.onvif_port != settings.ui_port:
        await web.TCPSite(runner, "0.0.0.0", settings.onvif_port).start()
    await sync_alias_sites(app)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            try:
                loop.add_signal_handler(getattr(signal, name), stop.set)
            except (NotImplementedError, RuntimeError):
                pass
    try:
        await stop.wait()
    finally:
        await runner.cleanup()
