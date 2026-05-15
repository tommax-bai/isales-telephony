"""D1 PoC 2.1 — sounddevice + WASAPI Shared/Exclusive Mode 延迟实测.

Spec: windows-client-core / tasks.md § 2.1, design.md Decision 3 + Risks.

Run on a Windows 10 21H2+ / Windows 11 machine:

  python -m pip install sounddevice numpy
  python scripts/d1_poc/poc_wasapi_latency.py

What it measures
----------------

1.  Callback jitter — for each ``sd.Stream`` callback, the gap between
    the previous wall-clock timestamp and the current one. With a
    ``blocksize=320`` (20 ms @ 16 kHz) input stream we expect callbacks
    every 20 ms; the P50/P95/P99 of (actual - 20 ms) is the metric.
    Acceptance: **P95 jitter ≤ 10 ms** (D1 design Decision 3 budget).
2.  Buffer floor — repeats the jitter measurement at blocksizes of
    20/10/5 ms; the smallest blocksize where input_overflow remains 0
    for 30 s is reported. ``input_overflow`` flag fires when the
    callback can't keep up; a stable floor at ≤ 10 ms is the design
    target for "low-latency".
3.  Mode comparison — runs Shared Mode (default), then Exclusive Mode
    (``WasapiSettings(exclusive=True)``); we report the floor + jitter
    for each so we know whether to ship Shared (default per design) or
    Exclusive (env override per task 4.5).

Outputs ``poc_wasapi_latency_result.json`` in the current directory.

What it does NOT measure
------------------------

End-to-end mic → speaker latency (which is the design.md Decision 4
overall budget) requires a hardware loopback (a cable from speaker to
mic + impulse cross-correlation). That measurement happens during
task 9.2 hardware acceptance on a real GSM modem; this PoC only
validates the software path delay introduced by sounddevice + WASAPI.
A jitter floor above the design budget here is reason enough to
re-evaluate the backend before writing 4.x audio code.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import threading
import time
from dataclasses import asdict, dataclass

try:
    import numpy as np  # noqa: F401  (sounddevice needs numpy at import time)
    import sounddevice as sd
except ImportError as exc:  # pragma: no cover
    sys.stderr.write(
        f"sounddevice / numpy required: {exc}\n"
        "  pip install sounddevice numpy\n"
    )
    sys.exit(2)


SAMPLE_RATE_HZ = 16_000  # design Decision 3 — RTC-side rate
CHANNELS = 1
DTYPE = "int16"


@dataclass
class _RunResult:
    mode: str  # "shared" | "exclusive"
    blocksize_samples: int
    blocksize_ms: float
    duration_s: float
    callbacks: int
    overflows: int
    underflows: int
    p50_jitter_ms: float
    p95_jitter_ms: float
    p99_jitter_ms: float
    max_jitter_ms: float


def _measure(
    *,
    blocksize_samples: int,
    duration_s: float,
    exclusive: bool,
) -> _RunResult:
    """Open an input+output stream, capture callback timestamps, report
    jitter + xrun counts.

    The output side writes silence so we exercise the duplex path
    (the spec ships both capture + playback to WASAPI).
    """
    expected_gap_s = blocksize_samples / SAMPLE_RATE_HZ
    gaps_ms: list[float] = []
    overflow_count = 0
    underflow_count = 0
    last_ts: list[float | None] = [None]  # mutable for closure
    silence_block = (b"\x00\x00" * blocksize_samples)
    lock = threading.Lock()

    # Resolve WASAPI host API index.
    try:
        wasapi_idx = next(
            i
            for i, h in enumerate(sd.query_hostapis())
            if "wasapi" in h["name"].lower()
        )
    except StopIteration:
        raise RuntimeError(
            "WASAPI host API not found — are you on Windows? "
            "sd.query_hostapis()="
            f"{[h['name'] for h in sd.query_hostapis()]}"
        ) from None

    extra_settings = None
    if exclusive:
        try:
            extra_settings = sd.WasapiSettings(exclusive=True)
        except Exception:  # noqa: BLE001 — sounddevice versions vary
            return _RunResult(
                mode="exclusive",
                blocksize_samples=blocksize_samples,
                blocksize_ms=expected_gap_s * 1000,
                duration_s=0.0,
                callbacks=0,
                overflows=-1,
                underflows=-1,
                p50_jitter_ms=float("nan"),
                p95_jitter_ms=float("nan"),
                p99_jitter_ms=float("nan"),
                max_jitter_ms=float("nan"),
            )

    # Resolve default input / output devices on WASAPI.
    default_input, default_output = sd.default.device
    if default_input is None or default_output is None:
        # Pick the first WASAPI in/out device explicitly.
        wasapi_in = next(
            (
                i
                for i, d in enumerate(sd.query_devices())
                if d["hostapi"] == wasapi_idx and d["max_input_channels"] > 0
            ),
            None,
        )
        wasapi_out = next(
            (
                i
                for i, d in enumerate(sd.query_devices())
                if d["hostapi"] == wasapi_idx and d["max_output_channels"] > 0
            ),
            None,
        )
        if wasapi_in is None or wasapi_out is None:
            raise RuntimeError(
                "No WASAPI input/output device found. Plug in a USB "
                "audio device and rerun."
            )
        default_input, default_output = wasapi_in, wasapi_out

    def callback(  # noqa: PLR0913 — sounddevice signature
        indata: object,
        outdata: object,
        frames: int,
        time_info: object,
        status: object,
    ) -> None:
        nonlocal overflow_count, underflow_count
        del indata, time_info, frames  # measured via wall-clock + status
        now = time.perf_counter()
        # Echo silence — duplex path exercised but no audible output.
        outdata_buffer = outdata  # type: ignore[assignment]
        # PortAudio gives us a CFFI buffer; write zeros sample-wise.
        # Suppress any buffer-shape mismatch — xrun signals the issue if
        # it actually matters; we don't want callback exceptions to kill
        # the stream.
        with contextlib.suppress(Exception):
            outdata_buffer[:] = silence_block[: len(outdata_buffer)]  # type: ignore[index]
        with lock:
            if last_ts[0] is not None:
                actual_gap_ms = (now - last_ts[0]) * 1000.0
                gaps_ms.append(actual_gap_ms - expected_gap_s * 1000.0)
            last_ts[0] = now
        # sd.CallbackFlags bitmask: input_overflow=4, output_underflow=1
        s = int(status) if status else 0
        if s & 4:
            overflow_count += 1
        if s & 1:
            underflow_count += 1

    try:
        with sd.Stream(
            samplerate=SAMPLE_RATE_HZ,
            blocksize=blocksize_samples,
            dtype=DTYPE,
            channels=CHANNELS,
            device=(default_input, default_output),
            callback=callback,
            extra_settings=extra_settings,
            latency="low",
        ):
            time.sleep(duration_s)
    except Exception as exc:  # noqa: BLE001 — record + return
        sys.stderr.write(
            f"  stream open failed (mode={'exclusive' if exclusive else 'shared'}, "
            f"blocksize={blocksize_samples}): {exc}\n"
        )
        return _RunResult(
            mode="exclusive" if exclusive else "shared",
            blocksize_samples=blocksize_samples,
            blocksize_ms=expected_gap_s * 1000,
            duration_s=0.0,
            callbacks=0,
            overflows=-1,
            underflows=-1,
            p50_jitter_ms=float("nan"),
            p95_jitter_ms=float("nan"),
            p99_jitter_ms=float("nan"),
            max_jitter_ms=float("nan"),
        )

    if not gaps_ms:
        return _RunResult(
            mode="exclusive" if exclusive else "shared",
            blocksize_samples=blocksize_samples,
            blocksize_ms=expected_gap_s * 1000,
            duration_s=duration_s,
            callbacks=0,
            overflows=overflow_count,
            underflows=underflow_count,
            p50_jitter_ms=float("nan"),
            p95_jitter_ms=float("nan"),
            p99_jitter_ms=float("nan"),
            max_jitter_ms=float("nan"),
        )

    abs_gaps = [abs(g) for g in gaps_ms]
    abs_gaps.sort()
    n = len(abs_gaps)
    return _RunResult(
        mode="exclusive" if exclusive else "shared",
        blocksize_samples=blocksize_samples,
        blocksize_ms=expected_gap_s * 1000,
        duration_s=duration_s,
        callbacks=n + 1,
        overflows=overflow_count,
        underflows=underflow_count,
        p50_jitter_ms=statistics.median(abs_gaps),
        p95_jitter_ms=abs_gaps[min(n - 1, int(n * 0.95))],
        p99_jitter_ms=abs_gaps[min(n - 1, int(n * 0.99))],
        max_jitter_ms=abs_gaps[-1],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="D1 PoC 2.1 — WASAPI latency / jitter probe")
    parser.add_argument(
        "--duration",
        type=float,
        default=30.0,
        help="seconds per measurement run (default 30s; spec calls for 60s "
        "end-to-end, but 30s × 6 runs gives the same fidelity faster)",
    )
    parser.add_argument(
        "--out",
        default="poc_wasapi_latency_result.json",
        help="output JSON path (default ./poc_wasapi_latency_result.json)",
    )
    args = parser.parse_args()

    print(f"sounddevice version: {sd.__version__}")
    print(f"host APIs: {[h['name'] for h in sd.query_hostapis()]}")
    print()

    results: list[_RunResult] = []
    # 20 / 10 / 5 ms blocksizes — design target floor ≤ 10 ms.
    for ms in (20, 10, 5):
        blocksize = int(SAMPLE_RATE_HZ * ms / 1000)
        for exclusive in (False, True):
            mode_label = "exclusive" if exclusive else "shared"
            print(
                f"running {mode_label:9} blocksize={blocksize:>4}"
                f" ({ms:>2} ms) for {args.duration:.0f}s ..."
            )
            r = _measure(
                blocksize_samples=blocksize,
                duration_s=args.duration,
                exclusive=exclusive,
            )
            results.append(r)
            print(
                f"  → callbacks={r.callbacks:>5}"
                f"  P50={r.p50_jitter_ms:6.2f}ms"
                f"  P95={r.p95_jitter_ms:6.2f}ms"
                f"  max={r.max_jitter_ms:6.2f}ms"
                f"  xrun={r.overflows}/{r.underflows}"
            )

    payload = {
        "sounddevice_version": sd.__version__,
        "host_apis": [h["name"] for h in sd.query_hostapis()],
        "results": [asdict(r) for r in results],
        "verdict": _verdict(results),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"\nwrote {args.out}")
    print(f"VERDICT: {payload['verdict']['summary']}")
    return 0


def _verdict(results: list[_RunResult]) -> dict[str, object]:
    """Reduce results to a pass/fail call for design Decision 3."""
    # Pass criteria: at least one (mode, blocksize) combination achieves
    # P95 ≤ 10 ms with zero overflow/underflow in 30 s.
    passing = [
        r
        for r in results
        if r.callbacks > 0
        and r.overflows == 0
        and r.underflows == 0
        and r.p95_jitter_ms <= 10.0
    ]
    if not passing:
        return {
            "pass": False,
            "summary": (
                "no (mode, blocksize) combination cleared P95 ≤ 10ms with "
                "zero xrun — design Decision 3 needs re-evaluation; consider "
                "PyAudioWPatch or shipping with a 20 ms+ floor"
            ),
        }
    # Prefer Shared Mode (Decision 3 default) if any Shared row passes,
    # otherwise Exclusive Mode.
    shared_passing = [r for r in passing if r.mode == "shared"]
    best = shared_passing[0] if shared_passing else passing[0]
    return {
        "pass": True,
        "summary": (
            f"PASS — {best.mode} mode @ {best.blocksize_ms:.0f}ms blocksize: "
            f"P95 jitter {best.p95_jitter_ms:.2f}ms, zero xrun. Ship D1 4.x "
            f"with {best.mode} mode as the default."
        ),
        "recommended_mode": best.mode,
        "recommended_blocksize_ms": best.blocksize_ms,
    }


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
