"""Exact echo-reference pattern: NON-BLOCKING read (timeout=0), tight loop,
write EXACTLY as many bytes as read. Report read amplitude per second.

The working reference (elementzonline) uses timeout=0 — read returns whatever
is available NOW (0..N), then writes the same count back, spinning fast. My
failed tests used BLOCKING reads (timeout 0.05/0.2) which stall the write for
up to 0.2s and desync the full-duplex PCM clock, starving the read (640B/s vs
16320B/s). This tests whether the non-blocking tight loop keeps the downlink
read alive (amp ~4000 when the caller speaks) — the real fix for the duplex
pump if so.

Usage: python read_amp_echo.py --at COM16 --audio COM17 --phone 13301035545
"""
import argparse
import array
import contextlib
import math
import struct
import time

import serial


def _max_abs_int16(buf: bytes) -> int:
    n = len(buf) - (len(buf) % 2)
    if n == 0:
        return 0
    a = array.array("h")
    a.frombytes(buf[:n])
    return max(abs(x) for x in a)


def send_at(at, cmd, t=5.0):
    at.reset_input_buffer()
    print(f"  >> {cmd}")
    at.write((cmd + "\r\n").encode("ascii"))
    end = time.time() + t
    while time.time() < end:
        ln = at.readline().decode("ascii", "replace").strip()
        if ln:
            print(f"  << {ln}")
            if ln in ("OK", "ERROR") or ln.startswith("+CME"):
                return ln
    return None


def wait_urc(at, target, t=30.0):
    end = time.time() + t
    while time.time() < end:
        ln = at.readline().decode("ascii", "replace").strip()
        if ln:
            print(f"  <- {ln}")
            if target in ln:
                return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--at", default="COM16")
    ap.add_argument("--audio", default="COM17")
    ap.add_argument("--secs", type=int, default=12)
    ap.add_argument("--echo", action="store_true", help="echo read bytes back (you hear yourself)")
    args = ap.parse_args()

    at = serial.Serial(args.at, 115200, timeout=1.0)
    # NON-BLOCKING read (timeout=0) — the echo reference's key setting.
    audio = serial.Serial(args.audio, 115200, timeout=0, write_timeout=0.5)
    call_up = False
    try:
        send_at(at, "AT", 2)
        with contextlib.suppress(Exception):
            send_at(at, "AT+CPCMREG=0", 2)
        print("[dial]")
        send_at(at, f"ATD{args.phone};", 10)
        call_up = True
        if not wait_urc(at, "VOICE CALL: BEGIN", 30):
            print("ERROR: no connect")
            return
        time.sleep(0.4)
        send_at(at, "AT+CPCMREG=1", 10)
        audio.reset_input_buffer()
        audio.reset_output_buffer()
        mode = "echo(听到自己)" if args.echo else "写等量 tone"
        print(f"\n>>> 接通!【非阻塞紧凑循环, {mode}】请持续说话 {args.secs}s <<<\n")
        phase = 0
        rbuf = bytearray()
        total = 0
        t_report = time.time() + 1.0
        sec = 0
        while sec < args.secs:
            r = audio.read(320)  # non-blocking → 0..320 bytes
            if r:
                rbuf.extend(r)
                total += len(r)
                if args.echo:
                    with contextlib.suppress(Exception):
                        audio.write(r)
                else:
                    ns = len(r) // 2
                    tone = struct.pack(
                        f"<{ns}h",
                        *[int(8000 * math.sin(2 * math.pi * 1000 * (phase + k) / 8000))
                          for k in range(ns)],
                    )
                    with contextlib.suppress(Exception):
                        audio.write(tone)
                    phase += ns
            if time.time() >= t_report:
                amp = _max_abs_int16(bytes(rbuf))
                tag = "<<<< 读到你的声音!" if amp > 600 else "(静音)" if amp < 200 else "(微弱)"
                print(f"  t={sec:2d}s  read={len(rbuf):5d}B  amp={amp:6d}  {tag}")
                rbuf.clear()
                t_report += 1.0
                sec += 1
        print(f"  total read = {total} bytes over {args.secs}s ({total // args.secs} B/s)")
        send_at(at, "AT+CPCMREG=0", 5)
    finally:
        if call_up:
            with contextlib.suppress(Exception):
                send_at(at, "ATH", 5)
        audio.close()
        at.close()
        print("[done]")


if __name__ == "__main__":
    main()
