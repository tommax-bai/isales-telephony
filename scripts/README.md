# `isales-telephony/scripts/`

Operational + smoke helper scripts. Not packaged with the wheel; run
out of a dev checkout.

## mac `--dev-no-modem` 端到端测试 (三件套)

跑通整条引擎对话链路 (AI 对 mac 扬声器开口 + mac mic barge-in)，**不依赖**真
GSM modem / Windows / 真拨号。三个脚本分工，目标是**把每次测试的人工分析成本降到一行命令**：

| 脚本 | 角色 | 何时用 |
|---|---|---|
| `mac_dev_no_modem_smoke.py` | **编排** preflight→mint JWT→freeze 探测→起 edge→dial→自动判读→清理 | 跑一整通 |
| `mac_dev_auto_analyze.py`   | **判读** 两端日志 → 9 检查点 PASS/WARN/FAIL | 看通没通 + 哪挂 |
| `call_timeline.py`          | **forensic** 两端日志 → wall-clock 时间线 + 延迟指标 | 看快慢 + 哪段延迟 |

### 一键跑 (smoke 编排)

```sh
.venv/bin/python scripts/mac_dev_no_modem_smoke.py
# 跑完自动判读 + 打印 9 检查点报告 + summary。CI/非交互加 --no-listen-check。
```

smoke 已替你处理两个手动起 edge 时**最容易漏的坑**：

- **app_id** — 从 `deploy/cloud/env/engine.env` 读 `ISALES_RTC_*` 注入 edge；漏了
  edge `production()` 会抛 `RtcError`，engine 独自在 RTC 房间收静音。
- **device-id** — 默认 `edge-01`，匹配 ECS `engine.env::ISALES_ENGINE_EDGE_DEVICE_ID`
  的 legacy 单 edge 路由；用别的 id 则 dial 路由不到本机 edge。

preflight 后会跑一次 **engine freeze 探测** (grpc Bidi `initial_metadata`)：engine
systemd `active` **不代表** event loop 没冻死 — 远端挂断的 hangup bug 会把整个 engine
asyncio loop 拖死、新连接全 hang。冻死则提示先 `systemctl restart isales-engine`。
`--skip-freeze-probe` 跳过。

### 单独判读一通 (analyze) — 核心，减少人工 grep

已有 call_id (或复盘历史通话)：

```sh
.venv/bin/python scripts/mac_dev_auto_analyze.py --call-id 168 --since '10:40:00'
.venv/bin/python scripts/mac_dev_auto_analyze.py --call-id 168 --json   # 机读
```

9 个检查点：`engine_health` / `engine_rtc_join` / `edge_rtc_join` / `greeting_tts` /
`uplink_audio` (检上行 stereo→mono downmix 削弱 + noise gate 是否把语音当噪音误滤) /
`downlink_audio` (检 ATS/GSLB 拦截导致的扬声器静音) / `mic_capture` / `asr_recognition`
(volcengine_asr_FINAL) / `hangup_finalize`。每点给 PASS/WARN/FAIL + 一句诊断，末尾汇总
指标 (上行 raw/final 峰值 vs gate=1500、下行 peak、mic 峰值、ATS 计数、首句 ASR、
hangup_cause)。

判读**按 call 段切割** (从 `dial_received` 那行到下个 dial / `session_finalized`) 隔离
多通 — 不靠 grep call_id (会同时漏抓本通无 id 的行如 tts/asr + 混入别通)。`--edge-log`
指向 edge 日志 (默认 `/tmp/isales-edge-mac-dev.log`)。

> ⚠️ 判读以**全量**日志为准。手动只 grep 几秒片段容易误判 (2026-06-08 踩过：片段看到
> `final=377`/`peak=3` 误判成"上行被滤/下行静音"，全量复盘发现那通其实识别了 7 句对话)。

### 起测前探 engine 冻死 (probe)

```sh
.venv/bin/python scripts/mac_dev_auto_analyze.py \
    --probe-engine --token-file /tmp/isales-edge-01.jwt
# 返回码 0=engine 活 / 3=冻死 (event loop hang，需重启)
```

### 事后看延迟 (timeline)

```sh
.venv/bin/python scripts/call_timeline.py --call-id 168 --asr-debug
```

合并 engine journalctl + edge log 成 wall-clock 时间线，标出真端到端延迟 (👤mic→🔊
扬声器) 与 "人说话却迟迟没回应" 的 ASR finalize 尾延迟空档。

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
