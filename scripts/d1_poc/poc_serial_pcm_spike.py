"""Live hardware spike: validate windows-client-core §4.7-§4.12 against
SIM7600G-H + COM11/12.

Non-destructive: no dial, no RTC join. Only:
  (1) find_audio_serial_path() resolves the audio COM from real comports()
  (2) AT+CPCMREG=? on AT channel returns (0-1)
  (3) AT+CPCMREG=1 outside a call returns ERROR -> PcmEnableError raised
  (4) AT+CPCMREG=0 outside a call returns ERROR -> tolerated (warning log)
  (5) WindowsSerialPcmCapture opens the audio COM
  (6) read_chunk returns within timeout (no bytes since CPCMREG=0)
  (7) clean teardown

Run: .venv/Scripts/python.exe scripts/d1_poc/poc_serial_pcm_spike.py
"""

from __future__ import annotations

import asyncio
import sys

import serial.tools.list_ports as lp

from isales_telephony.modem_controller.at_client import (
    PcmEnableError,
    SerialATClient,
)
from isales_telephony.modem_controller.audio.windows_serial_pcm import (
    DEFAULT_CHUNK_BYTES,
    WindowsSerialPcmCapture,
)
from isales_telephony.modem_controller.platforms.windows_serial import (
    find_audio_serial_path,
)

VID, PID = "1e0e", "9001"  # SIM7600G-H


def banner(msg: str) -> None:
    print(f"\n=== {msg} ===")


async def main() -> int:
    banner("1. comports() enumeration")
    at_port = None
    audio_port = None
    usb_serial = None
    for p in lp.comports():
        vid = f"{p.vid:04x}" if p.vid else None
        pid = f"{p.pid:04x}" if p.pid else None
        print(
            f"  {p.device:6} vid={vid} pid={pid} sn={p.serial_number!r:20} desc={p.description!r}"
        )
        if vid == VID and pid == PID:
            usb_serial = p.serial_number
            if p.description and "AT PORT" in p.description:
                at_port = p.device
            elif p.description and "Audio" in p.description:
                audio_port = p.device
    print(f"  -> AT_PORT={at_port}  AUDIO_PORT={audio_port}  USB_SERIAL={usb_serial!r}")
    if not at_port or not audio_port:
        print("  ! AT or Audio port missing; spike aborted (modem unplugged?)")
        return 1

    banner("2. find_audio_serial_path(usb_serial, vid, pid)")
    found = find_audio_serial_path(usb_serial=usb_serial, vid=VID, pid=PID)
    print(f"  -> returned {found!r}; expected {audio_port!r}")
    assert found == audio_port, f"mismatch: {found!r} vs {audio_port!r}"

    banner("3. SerialATClient.create_from_tty(AT_PORT) -> AT+CPCMREG=?")
    at = await SerialATClient.create_from_tty(at_port, baudrate=115200)
    try:
        r = await at._at.send("AT+CPCMREG=?", timeout=2.0)  # type: ignore[attr-defined]
        print(f"  CPCMREG=? lines: {r.lines}")
        assert any("0-1" in ln for ln in r.lines), r.lines

        banner("4. AT+CPCMREG=1 outside a call -> expect PcmEnableError")
        try:
            await at.cpcmreg_enable()
        except PcmEnableError as exc:
            print(f"  GOT PcmEnableError as expected: detail={exc.detail!r}")
        else:
            print("  ! cpcmreg_enable unexpectedly succeeded outside a call")
            return 2

        banner("4b. AT+CPCMREG=0 -> warning log path, no raise")
        await at.cpcmreg_disable()
        print("  cpcmreg_disable returned without raising")

        banner("5. WindowsSerialPcmCapture opens audio COM")
        cap = WindowsSerialPcmCapture(audio_port)
        try:
            cap.open_port()
            print(f"  capture opened on {audio_port}")

            banner("6. read_chunk() with CPCMREG=0 -> expect <= chunk bytes, no raise")
            buf = await asyncio.wait_for(cap.read_chunk(), timeout=1.0)
            print(f"  got {len(buf)} bytes (expected 0 since CPCMREG=0)")
            assert len(buf) <= DEFAULT_CHUNK_BYTES, len(buf)
        finally:
            await cap.close()
    finally:
        await at.aclose()

    banner("DONE - all assertions held")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
