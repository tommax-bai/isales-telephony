"""Non-destructive query of SIM7600 audio routing / rate / gain config.

READ (?) and TEST (=?) forms only — does NOT change any modem state.
Helps diagnose why downlink (far-end voice -> host read) is silent.
"""
import time

import serial


def at(s, cmd, t=1.5):
    s.reset_input_buffer()
    s.write((cmd + "\r\n").encode())
    end = time.time() + t
    out = []
    while time.time() < end:
        ln = s.readline().decode("ascii", "replace").strip()
        if ln:
            out.append(ln)
            if ln in ("OK", "ERROR") or ln.startswith("+CME"):
                break
    return out


def main():
    s = serial.Serial("COM12", 115200, timeout=1.0)
    cmds = [
        "AT+CPCMREG?", "AT+CPCMFRM?", "AT+CPCMFRM=?",
        "AT+CSDVC?", "AT+CSDVC=?",
        "AT+CRXVOL?", "AT+CTXVOL?",
        "AT+CRXGAIN?", "AT+CTXGAIN?", "AT+CMICGAIN?",
        "AT+CLVL?", "AT+COUTGAIN?",
        "AT+CPCMBANDWIDTH?", "AT+CPCMBANDWIDTH=?",
        "AT+CGMR",
    ]
    try:
        for c in cmds:
            print(f">> {c}")
            for ln in at(s, c):
                print(f"   << {ln}")
    finally:
        s.close()


if __name__ == "__main__":
    main()
