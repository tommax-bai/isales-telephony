"""
Validate the shared-handle + write-to-Audio-port fix using the REAL production
backends (WindowsSerialPcmCapture / Playback + adopt_serial_from), mirroring
main_windows.py wiring. Capture opens COM17 (Audio); playback adopts the same
handle; both pumps run concurrently on the one handle. Writes a 1kHz 8K tone
through playback.write_chunk while capture drains downlink.

PASS = the answered phone hears a continuous tone (uplink works via Audio port,
shared handle, concurrent read+write).

Usage: python validate_shared_handle_dial.py --at COM16 --audio COM17 --phone 13301035545
"""

import argparse
import asyncio
import math
import struct
import time

import serial

from isales_telephony.modem_controller.audio.windows_serial_pcm import (
    WindowsSerialPcmCapture,
    WindowsSerialPcmPlayback,
)


def send_at(at, cmd, timeout=5.0):
    at.reset_input_buffer()
    print(f"  >> {cmd}")
    at.write((cmd + "\r\n").encode("ascii"))
    end = time.time() + timeout
    while time.time() < end:
        ln = at.readline().decode("ascii", "replace").strip()
        if ln:
            print(f"  << {ln}")
            if ln in ("OK", "ERROR") or ln.startswith("+CME"):
                return ln
    return None


def wait_urc(at, target, timeout=30.0):
    print(f"  [waiting {target!r} ({timeout}s)]")
    end = time.time() + timeout
    while time.time() < end:
        ln = at.readline().decode("ascii", "replace").strip()
        if ln:
            print(f"  <- {ln}")
            if target in ln:
                return True
    return False


def tone(idx, n=160, freq=1000.0, sr=8000):
    return struct.pack(
        f"<{n}h",
        *[int(20000 * math.sin(2 * math.pi * freq * (idx + i) / sr)) for i in range(n)],
    )


async def run(args):
    at = serial.Serial(args.at, 115200, timeout=1.0)
    call_up = False
    cap = pb = None
    try:
        send_at(at, "AT", timeout=2.0)
        with __import__("contextlib").suppress(Exception):
            send_at(at, "AT+CPCMREG=0", timeout=2.0)  # clean prior state
        send_at(at, "AT+CPCMFRM=0", timeout=2.0)  # 8K (likely ERROR, suppressed)

        print("[dial]")
        send_at(at, f"ATD{args.phone};", timeout=10.0)
        call_up = True
        if not wait_urc(at, "VOICE CALL: BEGIN", timeout=30.0):
            print("ERROR: no connect")
            return
        await asyncio.sleep(0.4)
        send_at(at, "AT+CPCMREG=1", timeout=10.0)

        # --- production backends, shared handle ---
        cap = WindowsSerialPcmCapture(args.audio)
        pb = WindowsSerialPcmPlayback(args.audio)
        cap.open_port()
        pb.adopt_serial_from(cap)
        print(f"  shared handle on {args.audio}: cap._serial is pb._serial -> "
              f"{cap._serial is pb._serial}")

        stop = asyncio.Event()
        rstats = {"bytes": 0}

        async def reader():
            while not stop.is_set():
                d = await cap.read_chunk()
                rstats["bytes"] += len(d)

        async def writer():
            idx = 0
            i = 0
            t0 = time.time()
            while not stop.is_set():
                await pb.write_chunk(tone(idx))
                idx += 160
                i += 1
                nxt = t0 + (i + 1) * 0.020
                slp = nxt - time.time()
                if slp > 0:
                    await asyncio.sleep(slp)

        print(f"\n>>> LISTEN for ~{args.secs}s continuous tone on the phone <<<")
        rt = asyncio.create_task(reader())
        wt = asyncio.create_task(writer())
        await asyncio.sleep(args.secs)
        stop.set()
        await asyncio.gather(rt, wt, return_exceptions=True)
        print(f"\n[downlink] capture read {rstats['bytes']} bytes "
              f"(>0 => shared handle live for read while writing)")

        send_at(at, "AT+CPCMREG=0", timeout=5.0)
    finally:
        if pb is not None:
            await pb.close()
        if cap is not None:
            await cap.close()
        if call_up:
            with __import__("contextlib").suppress(Exception):
                send_at(at, "ATH", timeout=5.0)
        if at.is_open:
            at.close()
        print("[done]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--at", default="COM16")
    ap.add_argument("--audio", default="COM17")
    ap.add_argument("--secs", type=float, default=6.0)
    args = ap.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
