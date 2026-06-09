#!/usr/bin/env python3
"""SIM7600G-H capture-wedge probe + escalating recovery ladder.

Background (deploy/edge/windows/STATE.md § 9, commit 961e489, 2026-06-07)
------------------------------------------------------------------------
On the Windows edge the SIM7600G-H modem occasionally enters a *capture
bad-state*: the engine sees ``raw_max_rms=0`` (pure silence on the uplink)
for tens of seconds even though the audio pump is running and the downlink
is fine. The decisive isolation that night (a throwaway ``capture_downlink.py``
that read COM11 directly, bypassing daemon + cloud) showed the modem's own
capture path was dead and only a **physical USB replug** brought it back —
clean amplitude 4000-5000 @ 8 kHz afterwards.

STATE.md left one question open verbatim:

    真因 = modem 进了采集坏态，物理重插复位（`AT+CRESET` 软复位能否替代**待验**）

This script answers that question empirically. The physical replug works
because it resets BOTH layers at once — the host USB stack AND the modem
firmware. A software reset only touches one layer, so the only way to learn
which layer the wedge actually lives in is to try the cheapest reset first
and walk up until one sticks.

Two modes
---------
``probe``   Recreate the committed-only-as-a-memory ``capture_downlink.py``:
            discover the AT + audio COM ports, dial ``--number``, enable
            CPCMREG, read raw COM11 PCM for ``--seconds``, and report the
            peak amplitude + RMS. PASS if the uplink carries real audio.

``ladder``  Run ``probe`` at each rung of an escalating reset ladder, cheapest
            first, stopping at the first rung that restores capture. The rung
            that fixes it tells you which layer the wedge was in, and the
            result is appended to ``--results-file`` so repeated wedges build
            evidence over time.

The ladder, cheapest → most disruptive (and what each rung resets):

    0. baseline           — no reset; is capture actually wedged right now?
    1. CPCMREG re-arm      — re-open the PCM gate (CPCMREG=0→1). In-call,
                             no re-enumeration. Resets only the AT audio gate.
    2. CFUN=0→1            — baseband / RF re-init. Drops the call, KEEPS USB
                             enumeration. Resets the modem radio stack.
    3. AT+CRESET           — full module soft reset. Drops USB enumeration;
                             COM ports disappear + come back (possibly with new
                             COM numbers). THIS is the STATE.md 待验 rung.
    4. host USB reset      — the "unbind/rebind" rung. Linux: sysfs
                             unbind→bind. Windows: ``pnputil /restart-device``.
                             Resets the HOST side only (driver + USB pipe),
                             NOT the modem firmware.
    5. physical replug     — operator pulls + reinserts USB. Known-good
                             (resets everything). Manual; the script just
                             waits for re-enumeration.

Important: rung 4 resets the host side only — if the wedge is in the modem's
firmware capture engine (the more likely case given that even a pure COM11
read stayed silent on 2026-06-07), rung 4 may NOT fix it while rungs 2/3 do.
That asymmetry is exactly what this ladder is built to reveal.

Usage (run on the Windows edge box, inside the telephony venv):

    .venv/Scripts/python.exe scripts/modem_capture_recover.py probe  --number 13800138000
    .venv/Scripts/python.exe scripts/modem_capture_recover.py ladder --number 13800138000

The operator answers the dialled phone and keeps talking / making noise for
the whole measurement window — the uplink only carries amplitude while the
far end is making sound. After CFUN/CRESET/host-reset/replug the call drops,
so the script re-dials and the operator answers again.

Depends only on isales_telephony.modem_controller.{at_client, audio.
windows_serial_pcm, platforms.windows_serial} + pyserial — no IPC server, no
DB, no isales-engine, no cloud. Mirrors scripts/at_smoke.py +
scripts/d1_poc/poc_serial_pcm_spike.py conventions.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import platform
import subprocess
import sys
import time
import traceback
from array import array
from dataclasses import asdict, dataclass, field
from datetime import datetime

import serial.tools.list_ports as lp

from isales_telephony.modem_controller.at_client import (
    PcmEnableError,
    SerialATClient,
)
from isales_telephony.modem_controller.audio.windows_serial_pcm import (
    DEFAULT_BAUDRATE,
    WindowsSerialPcmCapture,
)
from isales_telephony.modem_controller.platforms.windows_serial import (
    find_audio_serial_path,
)

logger = logging.getLogger("modem_capture_recover")

# SIM7600G-H composite USB descriptor (v1.0 main SKU). Overridable via CLI for
# operator-sourced D1 modems. The descriptions "AT PORT" / "Audio" discriminate
# the MI_02 control port from the MI_04 serial-PCM audio port.
DEFAULT_VID = "1e0e"
DEFAULT_PID = "9001"

# int16 peak-amplitude PASS gate. 2026-06-07 ground truth: healthy uplink
# peaked at 4000-5000, the wedged state sat at ~0 for 40 s. 1000 is comfortably
# above the noise floor and below a real voice peak, so it cleanly separates
# "modem is feeding silence" from "modem is capturing audio".
PASS_AMP_THRESHOLD = 1000

# Seconds to wait for the modem's COM ports to re-appear after a reset that
# drops USB enumeration (CRESET / host-reset / replug). A SIM7600G-H cold
# reboot re-enumerates in ~10-20 s; 45 s leaves headroom.
REENUMERATE_TIMEOUT_S = 45.0


# --------------------------------------------------------------------------- #
# Port discovery
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Ports:
    at_port: str
    audio_port: str
    usb_serial: str | None


def discover_ports(vid: str, pid: str) -> Ports | None:
    """Resolve (AT, audio) COM ports for the modem by VID/PID + description.

    Returns None if either port is missing (modem unplugged / mid-reboot).
    COM numbers are dynamic across re-plugs, so this is re-run after every
    enumeration-dropping rung rather than cached.
    """
    at_port: str | None = None
    audio_port: str | None = None
    usb_serial: str | None = None
    for p in lp.comports():
        p_vid = f"{p.vid:04x}" if p.vid else None
        p_pid = f"{p.pid:04x}" if p.pid else None
        if p_vid != vid or p_pid != pid:
            continue
        usb_serial = p.serial_number
        desc = p.description or ""
        if "AT PORT" in desc:
            at_port = p.device
        elif "Audio" in desc:
            audio_port = p.device
    if not at_port or not audio_port:
        return None
    # Cross-check the audio port against the production resolver so the probe
    # exercises the same path the daemon uses (find_audio_serial_path).
    resolved = find_audio_serial_path(usb_serial=usb_serial, vid=vid, pid=pid)
    if resolved and resolved != audio_port:
        logger.warning(
            "audio port mismatch: scan=%s find_audio_serial_path=%s (using scan)",
            audio_port,
            resolved,
        )
    return Ports(at_port=at_port, audio_port=audio_port, usb_serial=usb_serial)


async def wait_for_reenumeration(vid: str, pid: str, budget_s: float) -> Ports | None:
    """Poll comports() until both modem ports re-appear, or the budget runs out."""
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        ports = discover_ports(vid, pid)
        if ports is not None:
            return ports
        await asyncio.sleep(0.5)
    return None


# --------------------------------------------------------------------------- #
# Amplitude measurement
# --------------------------------------------------------------------------- #
@dataclass
class ProbeResult:
    passed: bool
    max_amp: int
    rms: float
    samples: int
    per_second_peak: list[int] = field(default_factory=list)
    note: str = ""


def _amp_stats(buf: bytes) -> tuple[int, float, int]:
    """Peak |amplitude|, sum-of-squares, sample count for an int16-LE chunk."""
    usable = len(buf) - (len(buf) % 2)
    if usable == 0:
        return 0, 0.0, 0
    a = array("h")
    a.frombytes(buf[:usable])
    if sys.byteorder == "big":  # frames are little-endian on the wire
        a.byteswap()
    peak = 0
    sumsq = 0
    for s in a:
        v = s if s >= 0 else -s
        if v > peak:
            peak = v
        sumsq += s * s
    return peak, float(sumsq), len(a)


async def measure_capture(
    capture: WindowsSerialPcmCapture, seconds: float
) -> ProbeResult:
    """Read raw COM11 PCM for ``seconds`` and summarise amplitude.

    The per-second peak list mirrors the 2026-06-07 "逐秒 amp 4000-5000"
    log so a wedge (a run of zeros) is visible at a glance.
    """
    end = time.monotonic() + seconds
    sec_end = time.monotonic() + 1.0
    sec_peak = 0
    max_amp = 0
    sumsq_total = 0.0
    n_total = 0
    per_second: list[int] = []
    while time.monotonic() < end:
        buf = await capture.read_chunk()
        if buf:
            peak, sumsq, n = _amp_stats(buf)
            if peak > max_amp:
                max_amp = peak
            if peak > sec_peak:
                sec_peak = peak
            sumsq_total += sumsq
            n_total += n
        now = time.monotonic()
        if now >= sec_end:
            per_second.append(sec_peak)
            sec_peak = 0
            sec_end += 1.0
    if sec_peak:
        per_second.append(sec_peak)
    rms = (sumsq_total / n_total) ** 0.5 if n_total else 0.0
    passed = max_amp >= PASS_AMP_THRESHOLD
    note = (
        "uplink carries audio"
        if passed
        else f"uplink peak {max_amp} < {PASS_AMP_THRESHOLD} — capture wedged or far end silent"
    )
    return ProbeResult(
        passed=passed,
        max_amp=max_amp,
        rms=round(rms, 1),
        samples=n_total,
        per_second_peak=per_second,
        note=note,
    )


# --------------------------------------------------------------------------- #
# Call context: dial → connected → CPCMREG=1 → open capture
# --------------------------------------------------------------------------- #
@dataclass
class CallCtx:
    at: SerialATClient
    call_id: str
    capture: WindowsSerialPcmCapture
    stream_task: asyncio.Task
    hangup_seen: dict


async def establish_call(ports: Ports, number: str, dial_timeout: float) -> CallCtx:
    """Open AT, dial, wait for connect, arm CPCMREG, open the capture port."""
    at = await SerialATClient.create_from_tty(ports.at_port, baudrate=DEFAULT_BAUDRATE)
    connected = asyncio.Event()
    hangup_seen: dict = {}

    call_id, stream = await at.dial(number)

    async def _consume() -> None:
        async for ev in stream:
            logger.info("EVENT %s call_id=%s cause=%s", ev.event, ev.call_id, ev.cause)
            if ev.event == "connected":
                connected.set()
            elif ev.event == "remote_hangup":
                hangup_seen["ev"] = ev
                break

    task = asyncio.create_task(_consume())
    try:
        await asyncio.wait_for(connected.wait(), timeout=dial_timeout)
    except TimeoutError:
        task.cancel()
        with contextlib.suppress(Exception):
            await at.aclose()
        raise RuntimeError(
            f"call to {number!r} never reached 'connected' within {dial_timeout}s "
            "— did the operator answer?"
        ) from None

    # In-call now, so AT+CPCMREG=1 will return OK (it ERRORs outside a call).
    await at.cpcmreg_enable()
    capture = WindowsSerialPcmCapture(ports.audio_port)
    capture.open_port()
    return CallCtx(
        at=at, call_id=call_id, capture=capture, stream_task=task, hangup_seen=hangup_seen
    )


async def teardown_call(ctx: CallCtx) -> None:
    """Best-effort: close capture, hang up, cancel stream, close AT."""
    with contextlib.suppress(Exception):
        await ctx.capture.close()
    with contextlib.suppress(Exception):
        await ctx.at.cpcmreg_disable()
    if "ev" not in ctx.hangup_seen:
        with contextlib.suppress(Exception):
            await ctx.at.hangup(ctx.call_id)
    ctx.stream_task.cancel()
    with contextlib.suppress(Exception, asyncio.CancelledError):
        await ctx.stream_task
    with contextlib.suppress(Exception):
        await ctx.at.aclose()


# --------------------------------------------------------------------------- #
# Reset rungs
# --------------------------------------------------------------------------- #
@dataclass
class Rung:
    index: int
    name: str
    in_call: bool  # reset applied to the live call (vs out-of-call reset)
    needs_reenumerate: bool  # USB enumeration drops; ports must be re-discovered
    apply: object  # async callable; signature depends on in_call (see below)


async def _rung0_baseline(_ctx: CallCtx) -> None:
    """No reset — is capture wedged right now?"""
    return None


async def _rung1_cpcmreg(ctx: CallCtx) -> None:
    """Re-open the PCM gate without dropping the call (cheapest reset)."""
    await ctx.at.cpcmreg_disable()
    await asyncio.sleep(0.3)
    await ctx.at.cpcmreg_enable()


async def _rung2_cfun(ports: Ports) -> None:
    """CFUN=0→1 baseband re-init. Drops the call, keeps USB enumeration."""
    at = await SerialATClient.create_from_tty(ports.at_port, baudrate=DEFAULT_BAUDRATE)
    try:
        await at._at.send("AT+CFUN=0", timeout=10.0)  # type: ignore[attr-defined]
        await asyncio.sleep(2.0)
        await at._at.send("AT+CFUN=1", timeout=10.0)  # type: ignore[attr-defined]
        await asyncio.sleep(3.0)  # let the radio re-attach before re-dial
    finally:
        with contextlib.suppress(Exception):
            await at.aclose()


async def _rung3_creset(ports: Ports) -> None:
    """AT+CRESET full module soft reset. Drops USB enumeration (the 待验 rung).

    The modem may reboot before echoing OK, so a timeout/EOF here is expected,
    not an error — the re-enumeration wait that follows is the real signal.
    """
    at = await SerialATClient.create_from_tty(ports.at_port, baudrate=DEFAULT_BAUDRATE)
    try:
        with contextlib.suppress(Exception):
            await at._at.send("AT+CRESET", timeout=5.0)  # type: ignore[attr-defined]
    finally:
        with contextlib.suppress(Exception):
            await at.aclose()


async def _rung4_host_usb_reset(ports: Ports, vid: str, pid: str) -> None:
    """Host-side USB reset — the "unbind/rebind" rung. HOST layer only.

    Linux : sysfs driver unbind→bind on the device's bus-id (needs root).
    Windows: pnputil /restart-device on the modem's instance id.
    Else   : fall through to an operator prompt.

    Crucially this does NOT reset the modem firmware — if the wedge is in the
    modem's capture engine this rung will measure FAIL while CFUN/CRESET pass.
    """
    system = platform.system()
    if system == "Linux":
        await _linux_unbind_rebind(ports)
    elif system == "Windows":
        await _windows_restart_device(vid, pid)
    else:  # macOS / unknown — no host-side USB reset API worth trusting
        _prompt(
            f"[rung 4] No host USB-reset API on {system}. "
            "Treat as physical replug, or skip with --max-rung 3."
        )


async def _linux_unbind_rebind(ports: Ports) -> None:
    """echo <busid> > unbind ; echo <busid> > bind. Resolves busid from sysfs."""
    busid = _linux_busid_for_port(ports.at_port)
    if busid is None:
        _prompt(
            "[rung 4] could not resolve the USB bus-id from sysfs; "
            "run unbind/rebind manually then press Enter:\n"
            '  echo -n "<busid>" | sudo tee /sys/bus/usb/drivers/usb/unbind\n'
            '  echo -n "<busid>" | sudo tee /sys/bus/usb/drivers/usb/bind'
        )
        return
    drv = "/sys/bus/usb/drivers/usb"
    print(f"[rung 4] unbind/rebind busid={busid}")
    _run(["sudo", "sh", "-c", f'echo -n "{busid}" > {drv}/unbind'])
    await asyncio.sleep(1.0)
    _run(["sudo", "sh", "-c", f'echo -n "{busid}" > {drv}/bind'])


def _linux_busid_for_port(at_port: str) -> str | None:
    """Map /dev/ttyUSBn to its USB bus-id (e.g. '1-2') via /sys."""
    import glob
    import os

    name = os.path.basename(at_port)
    for tty in glob.glob(f"/sys/class/tty/{name}"):
        real = os.path.realpath(tty)  # .../usb1/1-2/1-2:1.2/ttyUSB0/tty/ttyUSB0
        for part in real.split("/"):
            # bus-id looks like '1-2' or '1-2.3'; ':' marks the interface dir
            if "-" in part and ":" not in part and part[0].isdigit():
                return part
    return None


async def _windows_restart_device(vid: str, pid: str) -> None:
    """pnputil /restart-device on the modem's parent USB instance id."""
    instance = _windows_instance_id(vid, pid)
    if instance is None:
        _prompt(
            f"[rung 4] could not find a USB instance for VID_{vid.upper()}&PID_{pid.upper()}; "
            "restart it manually in Device Manager (disable→enable) then press Enter, "
            "or run:\n  pnputil /restart-device \"<instance-id>\""
        )
        return
    print(f"[rung 4] pnputil /restart-device {instance}")
    _run(["pnputil", "/restart-device", instance])


def _windows_instance_id(vid: str, pid: str) -> str | None:
    """Find the modem's USB instance id via `pnputil /enum-devices`."""
    needle = f"VID_{vid.upper()}&PID_{pid.upper()}"
    try:
        out = subprocess.run(
            ["pnputil", "/enum-devices", "/connected"],
            capture_output=True,
            text=True,
            timeout=20,
        ).stdout
    except Exception:  # noqa: BLE001 — best-effort; caller prompts on None
        return None
    # Lines look like:  Instance ID:  USB\VID_1E0E&PID_9001\0123456789
    for line in out.splitlines():
        if "Instance ID" in line and needle in line.upper():
            return line.split(":", 1)[1].strip()
    return None


async def _rung5_replug(_ports: Ports) -> None:
    """Operator pulls + reinserts the USB modem (known-good full reset)."""
    _prompt("[rung 5] Unplug the USB modem, wait 3 s, plug it back in, then press Enter")


def _run(cmd: list[str]) -> None:
    try:
        subprocess.run(cmd, check=False, timeout=20)
    except Exception:  # noqa: BLE001 — surfaced via the re-enumeration wait
        logger.exception("command failed: %s", " ".join(cmd))


def _prompt(msg: str) -> None:
    print(msg)
    with contextlib.suppress(EOFError):
        input(">>> press Enter when done... ")


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
async def run_probe(ports: Ports, number: str, seconds: float, dial_timeout: float) -> ProbeResult:
    ctx = await establish_call(ports, number, dial_timeout)
    try:
        print(f"  connected; reading COM11 for {seconds:.0f}s (keep talking on the phone)...")
        return await measure_capture(ctx.capture, seconds)
    finally:
        await teardown_call(ctx)


def _print_result(label: str, r: ProbeResult) -> None:
    verdict = "PASS" if r.passed else "FAIL"
    print(
        f"  [{verdict}] {label}: max_amp={r.max_amp} rms={r.rms} "
        f"samples={r.samples} per_sec_peak={r.per_second_peak}"
    )
    print(f"         {r.note}")


async def run_ladder(
    vid: str,
    pid: str,
    number: str,
    seconds: float,
    dial_timeout: float,
    max_rung: int,
    results_file: str | None,
) -> int:
    rungs = [
        Rung(0, "baseline (no reset)", in_call=True, needs_reenumerate=False,
             apply=_rung0_baseline),
        Rung(1, "CPCMREG re-arm", in_call=True, needs_reenumerate=False,
             apply=_rung1_cpcmreg),
        Rung(2, "CFUN=0→1 baseband re-init", in_call=False, needs_reenumerate=False,
             apply=_rung2_cfun),
        Rung(3, "AT+CRESET module soft reset", in_call=False, needs_reenumerate=True,
             apply=_rung3_creset),
        Rung(4, "host USB reset (unbind/rebind)", in_call=False, needs_reenumerate=True,
             apply=None),
        Rung(5, "physical USB replug", in_call=False, needs_reenumerate=True,
             apply=_rung5_replug),
    ]

    ports = discover_ports(vid, pid)
    if ports is None:
        print("! modem AT/Audio ports not found — is it plugged in?", file=sys.stderr)
        return 1
    print(f"modem: AT={ports.at_port} AUDIO={ports.audio_port} SER={ports.usb_serial!r}")

    solved_at: Rung | None = None
    final_result: ProbeResult | None = None

    for rung in rungs:
        if rung.index > max_rung:
            break
        print(f"\n=== Rung {rung.index}: {rung.name} ===")

        if rung.in_call:
            # Apply the reset (or no-op) on a live call, then measure that call.
            ctx = await establish_call(ports, number, dial_timeout)
            try:
                await rung.apply(ctx)  # type: ignore[misc]
                result = await measure_capture(ctx.capture, seconds)
            finally:
                await teardown_call(ctx)
        else:
            # Out-of-call reset: nothing may hold the ports while we reset.
            if rung.index == 4:
                await _rung4_host_usb_reset(ports, vid, pid)
            else:
                await rung.apply(ports)  # type: ignore[misc]
            if rung.needs_reenumerate:
                print("  waiting for modem to re-enumerate...")
                ports = await wait_for_reenumeration(vid, pid, REENUMERATE_TIMEOUT_S)
                if ports is None:
                    print("  ! modem did not re-enumerate in time", file=sys.stderr)
                    return 2
                print(f"  re-enumerated: AT={ports.at_port} AUDIO={ports.audio_port}")
            print("  re-dialling for verification (answer the phone again)...")
            ctx = await establish_call(ports, number, dial_timeout)
            try:
                result = await measure_capture(ctx.capture, seconds)
            finally:
                await teardown_call(ctx)

        _print_result(rung.name, result)
        final_result = result
        if result.passed:
            solved_at = rung
            break

    _record_result(results_file, vid, pid, solved_at, final_result, max_rung)

    print("\n" + "=" * 60)
    if solved_at is None:
        print(f"UNRESOLVED: capture still wedged after rung {max_rung}.")
        print("If even rung 5 (physical replug) failed, the far end was likely")
        print("silent during measurement — re-run and keep talking the whole window.")
        return 3
    print(f"RESOLVED at rung {solved_at.index}: {solved_at.name}")
    if solved_at.index >= 4:
        print("→ wedge cleared only by a HOST-side reset → host USB / driver layer.")
    elif solved_at.index in (2, 3):
        print("→ wedge cleared by a MODEM reset (CFUN/CRESET) → modem firmware layer;")
        print("  a host-side unbind/rebind alone would NOT have been enough.")
    elif solved_at.index == 1:
        print("→ wedge cleared by re-arming the PCM gate → AT CPCMREG layer (cheapest).")
    else:
        print("→ no wedge: capture was healthy at baseline.")
    return 0


def _record_result(
    results_file: str | None,
    vid: str,
    pid: str,
    solved_at: Rung | None,
    result: ProbeResult | None,
    max_rung: int,
) -> None:
    if not results_file:
        return
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "vid": vid,
        "pid": pid,
        "host": platform.system(),
        "solved_rung": solved_at.index if solved_at else None,
        "solved_name": solved_at.name if solved_at else None,
        "max_rung_tried": max_rung,
        "final": asdict(result) if result else None,
    }
    try:
        with open(results_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        print(f"(result appended to {results_file})")
    except OSError as exc:
        logger.warning("could not write results file %s: %s", results_file, exc)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "mode", choices=["probe", "ladder"],
        help="probe = single capture check; ladder = escalating recovery",
    )
    parser.add_argument(
        "--number", required=True, help="Phone number to dial; operator answers + talks"
    )
    parser.add_argument(
        "--seconds", type=float, default=6.0,
        help="COM11 read window per measurement (default 6)",
    )
    parser.add_argument(
        "--dial-timeout", type=float, default=30.0,
        help="Seconds to wait for the answer (default 30)",
    )
    parser.add_argument(
        "--vid", default=DEFAULT_VID, help=f"USB VID hex (default {DEFAULT_VID} = SIM7600G-H)"
    )
    parser.add_argument("--pid", default=DEFAULT_PID, help=f"USB PID hex (default {DEFAULT_PID})")
    parser.add_argument(
        "--max-rung", type=int, default=5,
        help="Highest ladder rung to attempt (default 5; e.g. 3 to skip host-reset/replug)",
    )
    parser.add_argument(
        "--results-file", default="modem_recover_results.jsonl",
        help="JSONL file to append the outcome to (ladder mode)",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="AT-level debug logs")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        if args.mode == "probe":
            ports = discover_ports(args.vid, args.pid)
            if ports is None:
                print("! modem AT/Audio ports not found — is it plugged in?", file=sys.stderr)
                return 1
            print(f"modem: AT={ports.at_port} AUDIO={ports.audio_port} SER={ports.usb_serial!r}")
            result = asyncio.run(run_probe(ports, args.number, args.seconds, args.dial_timeout))
            _print_result("probe", result)
            return 0 if result.passed else 3
        return asyncio.run(
            run_ladder(
                vid=args.vid,
                pid=args.pid,
                number=args.number,
                seconds=args.seconds,
                dial_timeout=args.dial_timeout,
                max_rung=args.max_rung,
                results_file=args.results_file,
            )
        )
    except PcmEnableError as exc:
        print(
            f"! AT+CPCMREG=1 failed ({exc.detail!r}) — is the call actually connected?",
            file=sys.stderr,
        )
        return 4
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception:  # noqa: BLE001 — script entry; print + non-zero exit
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
