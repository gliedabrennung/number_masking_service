"""Standalone ARI application process, used when ``RUN_ARI_IN_API=false``.

Typical usage example:

    masking-ari
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app.ari import stasis
from app.core import config, logging_config
from app.db import cache, engine
from app.services import webhooks

log = logging_config.get_logger(__name__)


async def run() -> None:
    """Runs the Stasis application until SIGINT or SIGTERM arrives."""
    settings = config.get_settings()
    logging_config.configure_logging(settings.log_level, settings.log_json)
    for problem in settings.validate_production_secrets():
        log.warning("config.insecure_default", problem=problem)

    engine.init_engine(settings)
    cache.init_cache(settings)

    stasis_app = stasis.MaskingStasisApp(settings)
    task = asyncio.create_task(stasis_app.run(), name="stasis")

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    await stop.wait()
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await stasis_app.stop()
    await webhooks.drain()
    await cache.close_cache()
    await engine.dispose_engine()


def main() -> None:
    """Console entry point for ``masking-ari``."""
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
