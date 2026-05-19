# `isales-telephony/scripts/`

Operational + smoke helper scripts. Not packaged with the wheel; run
out of a dev checkout.

## `cloud_edge_smoke.py`

Direct gRPC bidi probe against the cloud `isales-engine`. Two modes.

### One-shot (default)

Quick "can I reach the cloud endpoint at all?" check — connect, send
one Heartbeat, idle 3 s, tear down. Exits non-zero if first connect
takes longer than `--timeout` (default 10 s).

```sh
.venv/bin/python scripts/cloud_edge_smoke.py \
    --endpoint 121.89.85.150:50051 \
    --token-file ~/.isales/edge-dev.jwt
```

### Soak — long-running idle-resilience check

Spec: `openspec/changes/cloud-edge-grpc-keepalive/specs/service-communication/
spec.md` § "smoke 工具支持长时间 soak 验证".

Runs for `--soak <N>` seconds, emits a Heartbeat every 30 s (matched
to the gRPC `keepalive_time_ms` channel option), records each
CONNECTED → next disconnect window as one "stream lifetime" sample.

Use this after deploying cloud-edge-grpc-keepalive (server + client +
Aliyun-side config) to verify that long-lived bidi streams survive
Aliyun stateful idle-cleanup.

```sh
.venv/bin/python scripts/cloud_edge_smoke.py \
    --endpoint 121.89.85.150:50051 \
    --token-file ~/.isales/edge-dev.jwt \
    --soak 600 --report-file /tmp/soak.json
```

Output: stream count, heartbeat send / failure counts, inbound frame
count, lifetime p50 / p95 / max / min. With `--report-file`, raw
stats are written to JSON.

**Acceptance gate**: `p95 lifetime ≥ 300 s` (constant
`SOAK_P95_TARGET_S` in the script). Exit code `0` iff the gate is
met; `3` if `p95 < 300 s` or no streams completed within the soak
window.

### Token

Both modes require a valid edge-device JWT. Mint on the cloud-side
host:

```sh
ssh root@121.89.85.150 'set -a; . /etc/isales/env/api.env; set +a; \
  /opt/isales/current/venv/bin/isales-edge-token-mint \
  --device-id edge-mac-dev-01 --ttl 24h 2>&1 | tail -1' \
> ~/.isales/edge-dev.jwt
chmod 600 ~/.isales/edge-dev.jwt
```

JWT is HS256-signed; the secret lives in `/etc/isales/env/api.env`
(same hash also in `engine.env`). Tokens themselves are sensitive —
don't paste them into chat / commits / issue trackers.
