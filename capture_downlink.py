"""Downlink (phone -> AI) capture + diagnosis.

Dials a number, enables PCM, then READS the modem audio port for N seconds
while the far end speaks. Reports per-second amplitude + cumulative byte rate
(to infer the real sample rate), and saves the raw PCM as BOTH an 8 kHz and a
16 kHz WAV so you can listen and tell which rate is the real far-end voice.

  16000 B/s  => 8 kHz int16 mono
  32000 B/s  => 16 kHz int16 mono   <-- suspected current downlink rate

Usage:
  python capture_downlink.py --phone 13301035545 --at COM12 --audio COM11 --secs 10
"""
import argparse
import array
import contextlib
import time
import wave

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
    out = []
    while time.time() < end:
        ln = at.readline().decode("ascii", "replace").strip()
        if ln:
            out.append(ln)
            print(f"  << {ln}")
            if ln in ("OK", "ERROR") or ln.startswith("+CME"):
                break
    return out


def wait_urc(at, target, t=30.0):
    end = time.time() + t
    while time.time() < end:
        ln = at.readline().decode("ascii", "replace").strip()
        if ln:
            print(f"  <- {ln}")
            if target in ln:
                return True
    return False


def write_wav(path, pcm, rate):
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    print(f"  saved {path}  ({len(pcm)} bytes @ {rate} Hz)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--at", default="COM12")
    ap.add_argument("--audio", default="COM11")
    ap.add_argument("--secs", type=int, default=10)
    args = ap.parse_args()

    at = serial.Serial(args.at, 115200, timeout=1.0)
    audio = serial.Serial(args.audio, 115200, timeout=0.05)  # 50ms read poll
    call_up = False
    raw = bytearray()
    try:
        send_at(at, "AT", 2)
        with contextlib.suppress(Exception):
            send_at(at, "AT+CPCMREG=0", 2)
        print("[dial]")
        send_at(at, f"ATD{args.phone};", 10)
        call_up = True
        if not wait_urc(at, "VOICE CALL: BEGIN", 30):
            print("ERROR: no connect (far end did not answer)")
            return
        time.sleep(0.4)
        send_at(at, "AT+CPCMREG=1", 10)
        audio.reset_input_buffer()
        print(f"\n>>> 接通!请对端【持续说话】{args.secs} 秒 <<<\n")
        deadline = time.time() + args.secs
        sec_end = time.time() + 1.0
        sec_buf = bytearray()
        sec = 0
        t0 = time.time()
        while time.time() < deadline:
            data = audio.read(4096)
            if data:
                raw.extend(data)
                sec_buf.extend(data)
            if time.time() >= sec_end:
                amp = _max_abs_int16(bytes(sec_buf))
                tag = "<<<< 有声音!" if amp > 600 else "(静音)" if amp < 200 else "(微弱)"
                print(f"  t={sec:2d}s  {len(sec_buf):6d}B/s  amp={amp:6d}  {tag}")
                sec_buf.clear()
                sec_end += 1.0
                sec += 1
        elapsed = time.time() - t0
        rate_bps = len(raw) / elapsed if elapsed else 0
        print(f"\n  total {len(raw)} bytes over {elapsed:.1f}s = {rate_bps:.0f} B/s")
        print(f"  => 推断采样率 ~= {rate_bps/2:.0f} Hz "
              f"({'8kHz' if rate_bps < 24000 else '16kHz'})")
        send_at(at, "AT+CPCMREG=0", 5)
    finally:
        if call_up:
            with contextlib.suppress(Exception):
                send_at(at, "ATH", 5)
        audio.close()
        at.close()
        if raw:
            write_wav("downlink_8k.wav", bytes(raw), 8000)
            write_wav("downlink_16k.wav", bytes(raw), 16000)
        print("[done] 用播放器听 downlink_8k.wav 和 downlink_16k.wav,哪个像正常语速人声?")


if __name__ == "__main__":
    main()
