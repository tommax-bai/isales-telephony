#!/usr/bin/env python3
"""Real-hardware smoke test for SerialATClient (impl-real-at).

Spec: device-hardware § "真硬件冒烟脚本".

Usage:
    python scripts/at_smoke.py --tty /dev/cu.usbmodem21301 --number 13800138000
    python scripts/at_smoke.py --tty /dev/ttyUSB-isales-modem --number 13800138000 \\
        --driver a7670 --wait-seconds 10

What it does:
  1. Open the tty + take fcntl flock + AT+GMI/AT+GMM auto-detect (or use hint)
  2. Place a voice call to <number> via ATD<number>;
  3. Stream every ATEvent (`connected` / `remote_hangup`) to stdout with a
     timestamp until the call ends
  4. After 'connected', wait <wait-seconds> (default 5), then call hangup()
     and confirm we receive a `remote_hangup` event with cause=local_clearing

Exits 0 if the dial → connect → hangup sequence completed cleanly; exits
non-zero on any exception (logged with traceback to stderr).

This script depends ONLY on isales_telephony.modem_controller.{at_client,
drivers, serial_protocol}. It does not start the IPC server, does not touch
the database, and does not depend on isales-engine — so a freshly-deployed
edge can run it as the first smoke test before promoting to production.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import traceback

from isales_telephony.modem_controller.at_client import (
    BusyDeviceError,
    SerialATClient,
)


async def run(tty: str, number: str, driver_hint: str, wait_seconds: float) -> int:
    t0 = time.monotonic()

    def _ts() -> str:
        return f"{time.monotonic() - t0:6.2f}s"

    print(f"[{_ts()}] opening {tty} (driver_hint={driver_hint or '<auto>'})")
    try:
        client = await SerialATClient.create_from_tty(tty, driver_hint=driver_hint)
    except BusyDeviceError as exc:
        print(f"[{_ts()}] BUSY: {exc}", file=sys.stderr)
        return 2
    print(f"[{_ts()}] modem ready; calling dial({number!r})")

    connected_seen = False
    hangup_event = None
    call_id: str | None = None
    try:
        call_id, stream = await client.dial(number)
        print(f"[{_ts()}] dial accepted; call_id={call_id}; polling for connect")

        hangup_scheduled = False

        async def _hangup_after_connect() -> None:
            print(f"[{_ts()}] connected; sleeping {wait_seconds}s then hanging up")
            await asyncio.sleep(wait_seconds)
            print(f"[{_ts()}] calling client.hangup({call_id})")
            assert call_id is not None
            await client.hangup(call_id)

        async for event in stream:
            print(f"[{_ts()}] EVENT {event.event} call_id={event.call_id} cause={event.cause!r}")
            if event.event == "connected":
                connected_seen = True
                if not hangup_scheduled:
                    hangup_scheduled = True
                    asyncio.create_task(_hangup_after_connect())
            elif event.event == "remote_hangup":
                hangup_event = event
                break
    finally:
        print(f"[{_ts()}] closing client")
        await client.aclose()

    print(f"[{_ts()}] DONE connected={connected_seen} hangup_cause={hangup_event.cause if hangup_event else None}")
    if hangup_event is None:
        print(f"[{_ts()}] FAIL: stream ended without remote_hangup", file=sys.stderr)
        return 3
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Real-hardware smoke test for SerialATClient")
    parser.add_argument("--tty", required=True, help="Serial device, e.g. /dev/cu.usbmodem21301")
    parser.add_argument("--number", required=True, help="Phone number to dial")
    parser.add_argument(
        "--driver",
        default="",
        choices=["", "a7670", "sim800c", "quectel_uc20"],
        help="Driver hint; empty means auto-detect via AT+GMI/AT+GMM",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=5.0,
        help="Seconds to hold the call after CONNECT before hanging up",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Show AT-level debug logs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        return asyncio.run(
            run(
                tty=args.tty,
                number=args.number,
                driver_hint=args.driver,
                wait_seconds=args.wait_seconds,
            )
        )
    except Exception:  # noqa: BLE001 — script entry; print + non-zero exit
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
