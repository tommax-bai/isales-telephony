"""Cloud-edge gRPC connectivity smoke test.

Two modes:

**One-shot** (default) — wires a real :class:`CloudEdgeGrpcClient` to
the cloud endpoint, waits for the bidi stream to go CONNECTED, sends
a Heartbeat, and tears down cleanly. Exits non-zero if the stream
never connects within ``--timeout`` s.

**Soak** (``--soak N``, cloud-edge-grpc-keepalive) — runs for N seconds,
sending a Heartbeat every 30 s to keep the stream active. Tracks each
successful stream's lifetime (CONNECTED → next stream_error). Prints
a distribution table at the end (count / p50 / p95 / max / min) and
optionally writes the raw stats to JSON via ``--report-file``. Use
this to verify the keepalive options actually penetrate Aliyun
stateful idle-cleanup (acceptance gate: p95 lifetime ≥ 300 s).

Run:
    .venv/bin/python scripts/cloud_edge_smoke.py \\
        --endpoint 121.89.85.150:50051 \\
        --token-file ~/.isales/edge-dev.jwt

    .venv/bin/python scripts/cloud_edge_smoke.py \\
        --endpoint 121.89.85.150:50051 \\
        --token-file ~/.isales/edge-dev.jwt \\
        --soak 600 --report-file /tmp/soak.json

This is intentionally NOT the full edge_main wiring — no modem, no
audio backends, no orchestrator. Purpose is to isolate cloud-edge gRPC
control-plane connectivity (TCP reach + token verification + bidi
stream up + idle-stream resilience) as one verifiable gate before
bringing the rest online.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from google.protobuf.timestamp_pb2 import Timestamp
from isales_common.proto import cloud_edge_pb2 as pb

from isales_telephony.transport.grpc_client import CloudEdgeGrpcClient

#: Heartbeat cadence inside `--soak` mode. Matched to the gRPC keepalive
#: ping interval (`grpc.keepalive_time_ms=30000`) so each iteration of
#: the soak loop is one logical "I'm alive" round-trip from edge to
#: cloud, on top of the protocol-level pings.
SOAK_HEARTBEAT_PERIOD_S: float = 30.0

#: Acceptance gate (spec § "smoke 工具支持长时间 soak 验证"). Soak runs
#: are expected to show p95 stream lifetime at or above this value
#: once the keepalive options are deployed on both sides + Aliyun
#: console-side config is correct.
SOAK_P95_TARGET_S: float = 300.0


def _now_ts() -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(datetime.now(tz=UTC))
    return ts


async def _one_shot(endpoint: str, token: str, timeout_s: float) -> int:
    """Single connect + heartbeat + 3s idle observe + clean shutdown."""
    client = CloudEdgeGrpcClient()
    inbound: list[pb.Cloud2Edge] = []

    async def on_cloud_msg(msg: pb.Cloud2Edge) -> None:
        kind = msg.WhichOneof("payload")
        print(f"  <- Cloud2Edge.{kind}", flush=True)
        inbound.append(msg)

    client.on_cloud_message(on_cloud_msg)

    print(f"==> connecting to {endpoint} ...", flush=True)
    await client.start(endpoint=endpoint, token=token)

    deadline = asyncio.get_running_loop().time() + timeout_s
    while not client.is_connected:
        if asyncio.get_running_loop().time() > deadline:
            print(
                f"FAIL: not CONNECTED after {timeout_s}s; bailing", flush=True
            )
            await client.stop()
            return 2
        await asyncio.sleep(0.2)
    print("==> CONNECTED", flush=True)

    print("==> sending Heartbeat (critical=True) ...", flush=True)
    hb = pb.Edge2Cloud(heartbeat=pb.Heartbeat(ts=_now_ts()))
    await client.send(hb, critical=True)
    print("==> Heartbeat sent OK", flush=True)

    print("==> idle 3s to observe any inbound frames ...", flush=True)
    await asyncio.sleep(3.0)

    print(f"==> total inbound frames: {len(inbound)}", flush=True)
    print("==> stopping client ...", flush=True)
    await client.stop()
    print("==> done", flush=True)
    return 0


async def _soak(
    endpoint: str,
    token: str,
    duration_s: float,
    report_file: str | None,
) -> int:
    """Soak the cloud-edge bidi stream for ``duration_s`` seconds.

    Records each connected → disconnected window as one "stream
    lifetime" sample. Reports distribution; optionally writes JSON.
    """
    client = CloudEdgeGrpcClient()
    inbound_count = 0

    async def on_cloud_msg(_msg: pb.Cloud2Edge) -> None:
        nonlocal inbound_count
        inbound_count += 1

    client.on_cloud_message(on_cloud_msg)

    print(f"==> soak target {endpoint} for {duration_s:.0f}s ...", flush=True)
    await client.start(endpoint=endpoint, token=token)

    soak_started_at = time.monotonic()
    deadline = soak_started_at + duration_s

    # Each entry: {"connected_at": monotonic, "disconnected_at": monotonic|None}
    lifetimes: list[dict[str, float | None]] = []
    current: dict[str, float | None] | None = None
    heartbeat_sends = 0
    heartbeat_failures = 0

    # Sample loop runs at 1 Hz: fast enough to catch disconnect/reconnect
    # edges and emit heartbeats on cadence, slow enough not to busy-spin.
    while time.monotonic() < deadline:
        connected_now = client.is_connected
        if connected_now and current is None:
            current = {
                "connected_at": time.monotonic(),
                "disconnected_at": None,
            }
            print(
                f"  [{time.monotonic() - soak_started_at:6.1f}s] CONNECTED",
                flush=True,
            )
        elif (not connected_now) and current is not None:
            current["disconnected_at"] = time.monotonic()
            lifetime = (
                current["disconnected_at"]
                - current["connected_at"]  # type: ignore[operator]
            )
            print(
                f"  [{time.monotonic() - soak_started_at:6.1f}s] "
                f"disconnected after {lifetime:.1f}s",
                flush=True,
            )
            lifetimes.append(current)
            current = None

        # Heartbeat cadence.
        if connected_now and heartbeat_sends * SOAK_HEARTBEAT_PERIOD_S <= (
            time.monotonic() - soak_started_at
        ):
            try:
                await client.send(
                    pb.Edge2Cloud(heartbeat=pb.Heartbeat(ts=_now_ts())),
                    critical=False,
                )
                heartbeat_sends += 1
            except Exception:  # noqa: BLE001 — record but keep soaking
                heartbeat_failures += 1

        await asyncio.sleep(1.0)

    # Close the in-flight window (if any) at deadline.
    if current is not None:
        current["disconnected_at"] = time.monotonic()
        lifetimes.append(current)

    await client.stop()

    durations = [
        (e["disconnected_at"] - e["connected_at"])  # type: ignore[operator]
        for e in lifetimes
        if e["disconnected_at"] is not None
    ]

    print(flush=True)
    print("==> soak summary", flush=True)
    print(f"     elapsed:           {time.monotonic() - soak_started_at:.1f}s",
          flush=True)
    print(f"     stream count:      {len(durations)}", flush=True)
    print(f"     heartbeat sends:   {heartbeat_sends}", flush=True)
    print(f"     heartbeat failed:  {heartbeat_failures}", flush=True)
    print(f"     inbound frames:    {inbound_count}", flush=True)

    if durations:
        sorted_d = sorted(durations)
        p50 = statistics.median(sorted_d)
        p95 = sorted_d[max(0, int(len(sorted_d) * 0.95) - 1)]
        max_d = max(sorted_d)
        min_d = min(sorted_d)
        print(f"     lifetime p50:      {p50:.1f}s", flush=True)
        print(f"     lifetime p95:      {p95:.1f}s "
              f"(acceptance gate: ≥ {SOAK_P95_TARGET_S:.0f}s)", flush=True)
        print(f"     lifetime max:      {max_d:.1f}s", flush=True)
        print(f"     lifetime min:      {min_d:.1f}s", flush=True)
        gate_pass = p95 >= SOAK_P95_TARGET_S
        print(
            f"     acceptance gate:   {'PASS' if gate_pass else 'FAIL'}",
            flush=True,
        )
    else:
        p50 = p95 = max_d = min_d = 0.0
        gate_pass = False
        print(f"     lifetime stats:    no completed streams",
              flush=True)
        print(f"     acceptance gate:   FAIL (no streams)", flush=True)

    if report_file:
        report = {
            "endpoint": endpoint,
            "soak_duration_s": duration_s,
            "elapsed_s": time.monotonic() - soak_started_at,
            "stream_count": len(durations),
            "heartbeat_sends": heartbeat_sends,
            "heartbeat_failures": heartbeat_failures,
            "inbound_frames": inbound_count,
            "lifetimes_s": durations,
            "p50_s": p50,
            "p95_s": p95,
            "max_s": max_d,
            "min_s": min_d,
            "acceptance_p95_target_s": SOAK_P95_TARGET_S,
            "acceptance_pass": gate_pass,
        }
        Path(report_file).write_text(
            json.dumps(report, indent=2), encoding="utf-8",
        )
        print(f"     report written:    {report_file}", flush=True)

    return 0 if gate_pass else 3


async def _main(args: argparse.Namespace, token: str) -> int:
    if args.soak is not None and args.soak > 0:
        return await _soak(
            args.endpoint, token, args.soak, args.report_file,
        )
    return await _one_shot(args.endpoint, token, args.timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument(
        "--timeout", type=float, default=10.0,
        help="One-shot mode: seconds to wait for first CONNECTED",
    )
    parser.add_argument(
        "--soak", type=float, default=None,
        help="Soak mode: total seconds to run. Mutually exclusive with "
             "default one-shot semantics.",
    )
    parser.add_argument(
        "--report-file", type=str, default=None,
        help="Soak mode only: write JSON report to this path.",
    )
    args = parser.parse_args()

    token = Path(args.token_file).read_text(encoding="ascii").strip()
    if not token:
        print(f"ERROR: token file {args.token_file} is empty", file=sys.stderr)
        return 1

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    return asyncio.run(_main(args, token))


if __name__ == "__main__":
    sys.exit(main())
