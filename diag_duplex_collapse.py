"""Reproduce the suspected root cause of "phone -> AI not working".

One call, two phases, far end speaks CONTINUOUSLY through both:

  Phase 1 (READ-ONLY)            : read 320B frames, no writes.  Baseline.
  Phase 2 (READ + WRITE-SILENCE) : exactly what run_duplex_pump does on
                                   Windows — read a frame, then write a
                                   320B silence frame, every iteration,
                                   on the SAME shared handle.

If downlink amplitude is healthy (~thousands) in Phase 1 but collapses
(~0) in Phase 2, the constant silence-write is corrupting the downlink
read => that is why "电话到AI" is silent while "AI到电话" works.

Usage:
  python diag_duplex_collapse.py --phone 13301035545 --at COM12 --audio COM11
"""
import argparse
import array
import contextlib
import time

import serial

FRAME = 320
SILENCE = b"\x00" * FRAME


def _amp(buf: bytes) -> int:
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
                break


def wait_urc(at, target, t=30.0):
    end = time.time() + t
    while time.time() < end:
        ln = at.readline().decode("ascii", "replace").strip()
        if ln:
            print(f"  <- {ln}")
            if target in ln:
                return True
    return False


def run_phase(name, audio, secs, *, write_silence):
    print(f"\n[{name}] {secs}s  write_silence={write_silence}")
    audio.reset_input_buffer()
    if write_silence:
        with contextlib.suppress(Exception):
            audio.reset_output_buffer()
    sec_end = time.time() + 1.0
    deadline = time.time() + secs
    sec_buf = bytearray()
    sec = 0
    while time.time() < deadline:
        data = audio.read(FRAME)
        if data:
            sec_buf.extend(data)
        if write_silence:
            with contextlib.suppress(Exception):
                audio.write(SILENCE)
        if time.time() >= sec_end:
            amp = _amp(bytes(sec_buf))
            tag = "<<<< 有声音" if amp > 600 else "(静音)" if amp < 200 else "(微弱)"
            print(f"  {name} t={sec}s  read={len(sec_buf):5d}B  amp={amp:6d}  {tag}")
            sec_buf.clear()
            sec_end += 1.0
            sec += 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--at", default="COM12")
    ap.add_argument("--audio", default="COM11")
    args = ap.parse_args()

    at = serial.Serial(args.at, 115200, timeout=1.0)
    audio = serial.Serial(args.audio, 115200, timeout=0.02, write_timeout=0.5)
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
        print("\n>>> 接通!请对端【全程持续说话】约 12 秒,中途不要停 <<<")
        run_phase("P1-READONLY", audio, 6, write_silence=False)
        run_phase("P2-READ+WRITE", audio, 6, write_silence=True)
        send_at(at, "AT+CPCMREG=0", 5)
    finally:
        if call_up:
            with contextlib.suppress(Exception):
                send_at(at, "ATH", 5)
        audio.close()
        at.close()
        print("\n[done] 对比 P1 vs P2 的 amp:若 P1 有声 P2 塌 → 静音写毁了下行读")


if __name__ == "__main__":
    main()
