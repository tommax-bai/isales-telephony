"""
SIM7600 COM11 audio direction diagnostic.

Single call, four phases, to settle: is COM11 the PCM data port, and which
directions actually move bytes?

  Phase A  read-only COM11  (2.5s) -> does downlink PCM arrive on COM11?
  Phase B  write-only COM11 (2.5s) -> can we push uplink PCM to COM11 at all,
                                      with NO concurrent reader (rules out the
                                      same-handle read/write interference)?
  Phase C  read-only COM10  (1.5s) -> is the diag port the real downlink port?
  Phase D  duplex  COM11    (2.5s) -> read+write together (production pattern)

Usage:
  python diag_com11_audio.py --phone 13301035545
"""

import argparse
import math
import struct
import sys
import threading
import time

import serial


def send_at(ser, cmd, timeout=5.0):
    ser.reset_input_buffer()
    print(f"  >> {cmd}")
    ser.write((cmd + "\r\n").encode("ascii"))
    lines = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode("ascii", errors="replace").strip()
        if not line:
            continue
        lines.append(line)
        print(f"  << {line}")
        if line in ("OK", "ERROR") or line.startswith("+CME ERROR"):
            break
    return lines


def wait_urc(ser, target, timeout=30.0):
    print(f"  [waiting for {target!r} ({timeout}s)...]")
    deadline = time.time() + timeout
    while time.time() < deadline:
        raw = ser.readline()
        if not raw:
            continue
        line = raw.decode("ascii", errors="replace").strip()
        if not line:
            continue
        print(f"  <- {line}")
        if target in line:
            return True
    return False


def tone_frame(freq, sr, n, idx):
    amp = 20000
    base = idx * n
    return struct.pack(
        f"<{n}h",
        *[int(amp * math.sin(2 * math.pi * freq * (base + i) / sr)) for i in range(n)],
    )


def read_phase(name, ser, seconds):
    print(f"\n[{name}] read-only for {seconds}s ...")
    ser.reset_input_buffer()
    total = 0
    nonzero = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        data = ser.read(320)
        if data:
            total += len(data)
            nonzero += 1
    print(f"[{name}] downlink: {total} bytes over {nonzero} non-empty reads "
          f"=> {'STREAMING' if total else 'SILENT (no bytes)'}")
    return total


def write_phase(name, ser, seconds, freq=1000.0, sr=8000, n=160, reader=False):
    print(f"\n[{name}] write-only for {seconds}s (concurrent_reader={reader}) ...")
    stop = threading.Event()
    rstats = {"bytes": 0}

    def _r():
        while not stop.is_set():
            try:
                d = ser.read(320)
            except Exception:
                break
            if d:
                rstats["bytes"] += len(d)

    rt = None
    if reader:
        rt = threading.Thread(target=_r, daemon=True)
        rt.start()

    ok = 0
    to = 0
    total_frames = int(seconds / 0.020)
    start = time.time()
    for i in range(total_frames):
        frame = tone_frame(freq, sr, n, i)
        try:
            written = ser.write(frame)
            if i < 3:
                print(f"    frame {i}: write() returned {written}")
            ok += 1
        except serial.SerialTimeoutException:
            to += 1
        nxt = start + (i + 1) * 0.020
        slp = nxt - time.time()
        if slp > 0:
            time.sleep(slp)
    if rt:
        stop.set()
        rt.join(timeout=1.0)
    print(f"[{name}] writes ok={ok} timeout={to} "
          f"=> {'WRITABLE' if ok and to == 0 else 'WRITE BLOCKED' if to else 'partial'}"
          + (f"; concurrent downlink read {rstats['bytes']} bytes" if reader else ""))
    return ok, to


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--at-port", default="COM12")
    ap.add_argument("--audio-port", default="COM11")
    ap.add_argument("--diag-port", default="COM10")
    args = ap.parse_args()

    print("=" * 60)
    print("SIM7600 COM11 audio-direction diagnostic")
    print(f"  AT={args.at_port}  AUDIO={args.audio_port}  DIAG={args.diag_port}")
    print("=" * 60)

    at = serial.Serial(args.at_port, 115200, timeout=1.0)
    audio = serial.Serial(args.audio_port, 115200, timeout=0.2, write_timeout=0.3)
    try:
        diag = serial.Serial(args.diag_port, 115200, timeout=0.2)
    except Exception as e:
        print(f"  (diag port {args.diag_port} open failed: {e}; skipping Phase C)")
        diag = None

    # Assert DTR/RTS — some USB modems gate the data endpoint on these.
    for s in (audio,):
        try:
            s.dtr = True
            s.rts = True
        except Exception:
            pass

    call_up = False
    try:
        print("\n[dial]")
        send_at(at, f"ATD{args.phone};", timeout=10.0)
        call_up = True
        if not wait_urc(at, "VOICE CALL: BEGIN", timeout=30.0):
            print("  no VOICE CALL: BEGIN; checking CLCC")
            r = send_at(at, "AT+CLCC", timeout=3.0)
            if not any(",0," in x for x in r):
                print("ERROR: call not active")
                return
        time.sleep(0.5)

        print("\n[CPCMREG=1]")
        send_at(at, "AT+CPCMREG=1", timeout=10.0)

        read_phase("A COM11", audio, 2.5)
        write_phase("B COM11", audio, 2.5, reader=False)
        if diag is not None:
            read_phase("C COM10", diag, 1.5)
        write_phase("D COM11", audio, 2.5, reader=True)

        print("\n[CPCMREG=0]")
        send_at(at, "AT+CPCMREG=0", timeout=5.0)
    finally:
        if call_up:
            try:
                send_at(at, "ATH", timeout=5.0)
            except Exception:
                pass
        for s in (audio, diag, at):
            if s is not None and s.is_open:
                s.close()
        print("\n[done]")


if __name__ == "__main__":
    main()
