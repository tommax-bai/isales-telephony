"""Cloud-edge gRPC connectivity smoke test.

One-shot script that wires a real :class:`CloudEdgeGrpcClient` to the
cloud endpoint with a real bearer token, waits for the bidi stream to
go CONNECTED, sends a Heartbeat, and tears down cleanly.

Run:
    .venv/Scripts/python.exe scripts/cloud_edge_smoke.py \\
        --endpoint 121.89.85.150:50051 \\
        --token-file .edge-token-test.jwt

Exits non-zero if the stream never goes CONNECTED within `--timeout` s,
or if the Heartbeat send raises EdgeNotConnected after connect.

This is intentionally NOT the full edge_main wiring — no modem, no
audio backends, no orchestrator. Purpose is to isolate cloud-edge gRPC
control-plane connectivity (TCP reach + token verification + bidi
stream up) as one verifiable gate before bringing the rest online.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from google.protobuf.timestamp_pb2 import Timestamp
from isales_common.proto import cloud_edge_pb2 as pb

from isales_telephony.transport.grpc_client import CloudEdgeGrpcClient


def _now_ts() -> Timestamp:
    ts = Timestamp()
    ts.FromDatetime(datetime.now(tz=UTC))
    return ts


async def _main(endpoint: str, token: str, timeout_s: float) -> int:
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    token = Path(args.token_file).read_text(encoding="ascii").strip()
    if not token:
        print(f"ERROR: token file {args.token_file} is empty", file=sys.stderr)
        return 1

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    return asyncio.run(_main(args.endpoint, token, args.timeout))


if __name__ == "__main__":
    sys.exit(main())
