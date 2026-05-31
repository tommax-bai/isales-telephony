"""
Re-test COM17 (audio) uplink write with variations not tried before:
  V1  rtscts=True  (hardware flow control — maybe modem gates OUT on CTS)
  V2  dsrdtr=True + DTR/RTS asserted, no rtscts
  V3  single-thread lockstep: read small -> write same small size, finite 2s
      write_timeout (so it can't hang forever like the blocking echo did)

Also prints the CTS/DSR/DSR line states on the audio port while writing — if
CTS is low under rtscts, the modem is signalling "not ready to receive",
which is a very different story from a dead driver.

NO file upload / CFTRANRX here (that wedged the modem last time). Pure writes.

Usage: python retest_audio_write.py --phone 13301035545 --at COM16 --audio COM17
"""

import argparse
import math
import struct
import time

import serial


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


def frame(idx, n=160, freq=1000.0, sr=8000):
    return struct.pack(
        f"<{n}h",
        *[int(20000 * math.sin(2 * math.pi * freq * (idx + i) / sr)) for i in range(n)],
    )


def variation(label, port, seconds, *, rtscts=False, dsrdtr=False,
              assert_lines=False, lockstep=False, wtimeout=1.0):
    print(f"\n[{label}] open {port} rtscts={rtscts} dsrdtr={dsrdtr} "
          f"lockstep={lockstep} wtimeout={wtimeout}")
    try:
        s = serial.Serial(port, 115200, timeout=0.05, write_timeout=wtimeout,
                          rtscts=rtscts, dsrdtr=dsrdtr)
    except Exception as e:
        print(f"  open failed: {e}")
        return
    if assert_lines:
        try:
            s.dtr = True
            s.rts = True
        except Exception as e:
            print(f"  line-assert err: {e}")
    try:
        print(f"  line states: cts={s.cts} dsr={s.dsr} ri={s.ri} cd={s.cd}")
    except Exception as e:
        print(f"  line-read err: {e}")

    ok = to = idx = i = 0
    start = time.time()
    while time.time() < start + seconds:
        if lockstep:
            r = s.read(320)  # drain whatever downlink is available first
        try:
            s.write(frame(idx))
            ok += 1
        except serial.SerialTimeoutException:
            to += 1
        idx += 160
        i += 1
        if not lockstep:
            nxt = start + (i + 1) * 0.020
            if nxt - time.time() > 0:
                time.sleep(nxt - time.time())
    try:
        print(f"  line states after: cts={s.cts} dsr={s.dsr}")
    except Exception:
        pass
    s.close()
    print(f"[{label}] ok={ok} timeout={to} "
          f"=> {'ACCEPTS <<<<<' if ok and not to else 'BLOCKED' if to else '?'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--at", default="COM16")
    ap.add_argument("--audio", default="COM17")
    args = ap.parse_args()

    at = serial.Serial(args.at, 115200, timeout=1.0)
    call_up = False
    try:
        send_at(at, "AT", timeout=2.0)
        print("[dial]")
        send_at(at, f"ATD{args.phone};", timeout=10.0)
        call_up = True
        if not wait_urc(at, "VOICE CALL: BEGIN", timeout=30.0):
            print("ERROR: no connect")
            return
        time.sleep(0.4)
        print("[CPCMREG=1]")
        send_at(at, "AT+CPCMREG=1", timeout=10.0)

        variation("V1 rtscts", args.audio, 3.0, rtscts=True)
        variation("V2 dsrdtr+lines", args.audio, 3.0, dsrdtr=True, assert_lines=True)
        variation("V3 lockstep", args.audio, 3.0, lockstep=True, wtimeout=2.0)

        print("\n[CPCMREG=0]")
        send_at(at, "AT+CPCMREG=0", timeout=5.0)
    finally:
        if call_up:
            try:
                send_at(at, "ATH", timeout=5.0)
            except Exception:
                pass
        if at.is_open:
            at.close()
        print("[done]")


if __name__ == "__main__":
    main()
