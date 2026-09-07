from __future__ import annotations
import asyncio
import logging
import sys

from app.device.manager import main as device_main


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    try:
        asyncio.run(device_main())
    except KeyboardInterrupt:
        logging.info("Interrumpido por usuario")
    except Exception as e:
        logging.exception("Error fatal: %s", e)
        sys.exit(1)