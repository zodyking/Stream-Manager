"""Local and add-on entrypoint: python -m onvif_proxy"""

from __future__ import annotations

import asyncio
import logging
import sys

from .config import load_settings
from .server import serve


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    settings = load_settings()
    logging.getLogger("onvif_proxy").info(
        "data %s ui %s onvif %s rtsp %s supervisor %s",
        settings.data_dir,
        settings.ui_port,
        settings.onvif_port,
        settings.rtsp_port,
        settings.supervisor,
    )
    try:
        asyncio.run(serve(settings))
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
