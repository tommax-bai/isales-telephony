"""modem-controller asyncio daemon entrypoint.

PR #2 (skeleton) only runs the event loop; PR #6 adds the IPC server, PR #7
the AT client + dial mock, PR #8 the udev watcher.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("isales.modem_controller")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    logger.info("modem-controller started (skeleton; PR #6 wires the IPC server)")
    # Block until the process is signalled. Subsequent PRs replace this with the
    # IPC server / udev watcher gather().
    await asyncio.Event().wait()


def run() -> None:
    """Console-script entry point: ``modem-controller``."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("modem-controller stopped")
