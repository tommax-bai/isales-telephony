"""Dial via AT, then READ the modem audio port and report amplitude per second.

Directly answers: does the SIM7600 USB PCM downlink (modem->host read) contain
the FAR-END (callee) voice, or only silence? If amp stays ~0 while the answerer
speaks, the modem isn't routing GSM-RX audio into the USB PCM read stream — a
modem audio-path config issue, independent of the edge daemon / engine.

Usage: python read_farend_amp.py --at COM16 --audio COM17 --phone 13301035545
"""
import argparse
import array
import contextlib
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
    args = ap.parse_args()

    at = serial.Serial(args.at, 115200, timeout=1.0)
    audio = serial.Serial(args.audio, 115200, timeout=0.2)
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
        print(f"\n>>> 接通!请【持续说话】{args.secs} 秒,每秒报采集振幅 <<<\n")
        for sec in range(args.secs):
            end = time.time() + 1.0
            buf = bytearray()
            while time.time() < end:
                buf.extend(audio.read(320))
            amp = _max_abs_int16(bytes(buf))
            tag = "<<<< 有你的声音!" if amp > 600 else "(静音)" if amp < 200 else "(微弱)"
            print(f"  t={sec:2d}s  read={len(buf):5d}B  amp={amp:6d}  {tag}")
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
