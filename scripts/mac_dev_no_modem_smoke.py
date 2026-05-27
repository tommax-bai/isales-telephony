"""mac --dev-no-modem 端到端演练: AI 对 mac 开口 + mac mic barge-in.

Spec: joint-mvp-gate-13301035545 § 6 (dev 等价路径); project_macos_dev_path
      § "dev 启动 / 关停命令". 走通整个引擎对话流程, **不依赖**真 GSM
      modem / Windows / 真拨号 — 13301035545 仅作 PG lead 字段,扬声器+
      mic 即是"通话"两端。

Mock 路径 (绕开 scheduler / device / sim_card / device_sim_binding):

    1. 本机 spawn ``isales-telephony-edge --dev-no-modem`` 子进程
       (DingRTC vendor framework 在 ~/codes/vendor/DingRTC_macOS_SDK_3_9_0/)
    2. ssh ECS mint 1h JWT for ``edge-01`` (matches engine.env
       ``ISALES_ENGINE_EDGE_DEVICE_ID=edge-01``)
    3. tail edge log 等 ``edge_daemon_started_dev_no_modem`` ready signal
       + ``cloud_edge_stream_opened`` (mac edge ↔ ECS engine bidi 通)
    4. ssh ECS scp + run ``_remote_inject_dial.py`` →
       ``LPUSH engine:dial`` 一条 ``DialRequest(lead_id=2, campaign_id=1,
       phone=13301035545)``;engine ``dial_consumer.dial_loop`` 立即 BLPOP
    5. engine ``RtcTelephonyClient.dial`` joins DingRTC + ``Cloud2Edge.
       DialCommand`` 给 ``edge-01`` (我们的 mac edge)
    6. mac edge ``orchestrator._on_dial_dev_no_modem`` → ``rtc_session.
       join`` (``MacosDingRtcPyObjCSession``) → 同 RTC 房间
    7. AI 状态机 GREETING → LLM → TTS → push_audio → DingRTC →
       mac edge ``audio_frames()`` → CoreAudio playback → mac 扬声器
    8. mac mic → CoreAudio capture → push_audio → DingRTC → engine
       ``audio_frames()`` → ASR (volcengine) → LLM → TTS → 回声循环
    9. ssh ECS poll ``call_record WHERE lead_id=2`` 看 status flow
    10. listen-check 交互: 用户人耳判断"是否听到 AI 开场白" yes/no

Audit ground truth (2026-05-27):
    - ✅ engine ``ISALES_ENGINE_TELEPHONY_MODE=rtc`` + ``RTC_SDK_KIND=vendor``
    - ✅ ECS 4 systemd active (api / engine / scheduler / worker)
    - ✅ provider_credential: volcengine (app_key+app_token) + dashscope
      (api_key) — ASR + TTS + LLM 全覆盖
    - ✅ lead 2 phone=13301035545 + campaign 1 "测试任务" 在 PG
    - ✅ ``call_record`` 只 FK 到 ``lead.id + campaign.id`` (不需 device
      chain seed)
    - ✅ DingRTC § 5.4 cloud + § 8.8 mac dual-peer 实证过 wire (channel
      ``rtc-smoke-1779785584`` / ``dual-peer-1779788945``)

Usage::

    python scripts/mac_dev_no_modem_smoke.py             # full e2e flow
    python scripts/mac_dev_no_modem_smoke.py --dry-run   # print intent
    python scripts/mac_dev_no_modem_smoke.py --skip-edge-start  # edge daemon already running externally
    python scripts/mac_dev_no_modem_smoke.py --timeout 90       # poll longer
    python scripts/mac_dev_no_modem_smoke.py --no-listen-check  # CI / non-interactive

Removal trigger: 真拨号 ``joint-mvp-gate-13301035545`` MVP 闭合 + Windows
edge ready 后,本脚本演化为 ``joint_mvp_dial.py`` 的 mac 子模式或归档。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ---------- constants -------------------------------------------------------

DEFAULT_SSH_HOST = "root@121.89.85.150"
DEFAULT_SSH_KEY = os.path.expanduser("~/codes/isales-4.pem")
DEFAULT_EDGE_DEVICE_ID = "edge-01"  # matches engine.env::ISALES_ENGINE_EDGE_DEVICE_ID
DEFAULT_CAMPAIGN_ID = 1
DEFAULT_LEAD_ID = 2
DEFAULT_PHONE = "13301035545"
DEFAULT_CALLER_ID = "13800138000"
DEFAULT_DEVICE_ID = 1  # opaque forward field; engine 不查 PG device 表

DEFAULT_RTC_FRAMEWORK = os.path.expanduser(
    "~/codes/vendor/DingRTC_macOS_SDK_3_9_0/DingRTC.framework"
)
META_REPO_ENGINE_ENV = "/Users/bears/codes/isales/deploy/cloud/env/engine.env"
TELEPHONY_VENV_PY = "/Users/bears/codes/isales-telephony/.venv/bin/python"
TELEPHONY_EDGE_BIN = (
    "/Users/bears/codes/isales-telephony/.venv/bin/isales-telephony-edge"
)
EDGE_LOG_PATH = Path("/tmp/isales-edge-mac-dev.log")
LOCAL_JWT_PATH = Path("/tmp/isales-edge-01.jwt")
REMOTE_INJECT_PATH = "/tmp/_remote_inject_dial.py"

# ECS-side artifacts (in /opt/isales/current)
ECS_DB_URL = (
    "postgresql+asyncpg://isales:***REMOVED***@127.0.0.1:5432/isales"
)
ECS_REDIS_URL = "redis://:***REMOVED***@127.0.0.1:6379/0"
ECS_VENV_PY = "/opt/isales/current/venv/bin/python"
ECS_MINT_JWT = "/opt/isales/current/venv/bin/isales-edge-token-mint"
ECS_PSQL = (
    "PGPASSWORD='***REMOVED***' "
    "psql -h 127.0.0.1 -U isales -d isales -t -A -c"
)


# ---------- helpers ---------------------------------------------------------


def ssh_cmd(host: str, key: str, remote: str, *, timeout: int = 30) -> str:
    """Run a remote command via ssh; non-0 exit → SystemExit."""
    cmd = [
        "ssh", "-i", key, "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=no", host, remote,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        sys.exit(
            f"ssh exec failed (rc={proc.returncode}):\n"
            f"  cmd: {remote}\n"
            f"  stderr: {proc.stderr.strip()}"
        )
    return proc.stdout.rstrip("\n")


def scp_to(host: str, key: str, local: Path, remote: str) -> None:
    cmd = [
        "scp", "-i", key, "-o", "StrictHostKeyChecking=no",
        str(local), f"{host}:{remote}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        sys.exit(f"scp failed: {proc.stderr.strip()}")


# ---------- phases ----------------------------------------------------------


def phase_preflight(args: argparse.Namespace) -> None:
    """4 cloud systemd + DingRTC + lead/campaign + provider_credential."""
    print("[1/6] pre-flight ...", file=sys.stderr)

    # 1. cloud services
    out = ssh_cmd(
        args.ssh_host, args.ssh_key,
        "systemctl is-active isales-api isales-engine isales-scheduler isales-worker"
    )
    states = out.split("\n")
    if any(s.strip() != "active" for s in states):
        sys.exit(f"  ✗ cloud services not all active: {states}")
    print("  ✓ cloud services all active (api/engine/scheduler/worker)", file=sys.stderr)

    # 2. DingRTC framework local
    fw_bin = Path(args.rtc_framework) / "DingRTC"
    if not fw_bin.exists():
        sys.exit(f"  ✗ DingRTC.framework not found at {args.rtc_framework}")
    print(f"  ✓ DingRTC.framework at {args.rtc_framework}", file=sys.stderr)

    # 3. edge daemon binary local
    if not Path(TELEPHONY_EDGE_BIN).exists():
        sys.exit(f"  ✗ isales-telephony-edge not in {TELEPHONY_EDGE_BIN}")
    print(f"  ✓ isales-telephony-edge venv-installed", file=sys.stderr)

    # 4. PG lead + campaign rows exist (FK targets for reserve_call_record)
    lead_check = ssh_cmd(
        args.ssh_host, args.ssh_key,
        f"{ECS_PSQL} \"SELECT id, phone FROM lead WHERE id={args.lead_id};\""
    )
    if not lead_check or "|" not in lead_check:
        sys.exit(f"  ✗ lead id={args.lead_id} not found in PG")
    print(f"  ✓ PG lead {args.lead_id}: {lead_check}", file=sys.stderr)

    campaign_check = ssh_cmd(
        args.ssh_host, args.ssh_key,
        f"{ECS_PSQL} \"SELECT id, name FROM campaign WHERE id={args.campaign_id};\""
    )
    if not campaign_check or "|" not in campaign_check:
        sys.exit(f"  ✗ campaign id={args.campaign_id} not found in PG")
    print(f"  ✓ PG campaign {args.campaign_id}: {campaign_check}", file=sys.stderr)

    # 5. provider_credential coverage (volcengine ASR+TTS + dashscope/volcengine LLM)
    cred_count = ssh_cmd(
        args.ssh_host, args.ssh_key,
        f"{ECS_PSQL} \"SELECT provider_id, count(*) FROM provider_credential "
        f"WHERE provider_id IN ('volcengine','dashscope','openai') "
        f"GROUP BY provider_id ORDER BY provider_id;\""
    )
    print(f"  ✓ provider_credential coverage:\n      {cred_count.replace(chr(10), chr(10)+'      ')}", file=sys.stderr)


def phase_mint_jwt(args: argparse.Namespace) -> str:
    """ssh ECS mint 1h JWT for edge-01; save to local /tmp."""
    print(f"[2/6] mint JWT (edge-id={args.edge_device_id}, ttl={args.ttl}) ...", file=sys.stderr)
    remote = (
        f"set -a; source /etc/isales/env/api.env; set +a; "
        f"{ECS_MINT_JWT} --device-id {args.edge_device_id} --ttl {args.ttl}"
    )
    jwt = ssh_cmd(args.ssh_host, args.ssh_key, remote)
    if not jwt or len(jwt) < 50 or jwt.count(".") != 2:
        sys.exit(f"  ✗ unexpected JWT format: {jwt[:60]}...")
    LOCAL_JWT_PATH.write_text(jwt + "\n")
    LOCAL_JWT_PATH.chmod(0o600)
    print(f"  ✓ JWT minted, saved to {LOCAL_JWT_PATH}", file=sys.stderr)
    return jwt


def phase_start_edge(args: argparse.Namespace, jwt: str) -> subprocess.Popen[bytes]:
    """spawn isales-telephony-edge --dev-no-modem; tail log for ready signal."""
    print("[3/6] starting edge daemon ...", file=sys.stderr)

    # load engine.env to get ISALES_RTC_APP_ID/KEY (mac edge mints its own RTC token)
    env_overrides: dict[str, str] = {}
    if Path(META_REPO_ENGINE_ENV).exists():
        for line in Path(META_REPO_ENGINE_ENV).read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.startswith("ISALES_RTC_") or k == "ISALES_JWT_SECRET":
                env_overrides[k] = v

    env = {
        **os.environ,
        "ISALES_CLOUD_EDGE_ENDPOINT": args.ecs_endpoint,
        "ISALES_EDGE_DEVICE_TOKEN": jwt,
        "ISALES_MACOS_DINGRTC_FRAMEWORK_PATH": args.rtc_framework,
        "ISALES_LOG_LEVEL": "INFO",
        **env_overrides,
    }

    ts = int(time.time())
    cmd = [
        TELEPHONY_EDGE_BIN, "--dev-no-modem",
        "--dev-channel", f"mac-e2e-{ts}",
        "--dev-uid", "mac-dev-01",
        "--dev-peer-uid", "engine-mac-01",
    ]
    log_fh = EDGE_LOG_PATH.open("wb")
    proc = subprocess.Popen(
        cmd, stdout=log_fh, stderr=subprocess.STDOUT, env=env,
        start_new_session=True,
    )
    print(f"  ✓ spawned pid={proc.pid}, log → {EDGE_LOG_PATH}", file=sys.stderr)

    # tail log for ready signal — timeout 30s
    deadline = time.time() + 30
    ready = False
    grpc_up = False
    while time.time() < deadline:
        if proc.poll() is not None:
            sys.exit(
                f"  ✗ edge daemon exited early (rc={proc.returncode}); "
                f"see {EDGE_LOG_PATH}"
            )
        log = EDGE_LOG_PATH.read_text(errors="replace") if EDGE_LOG_PATH.exists() else ""
        if "edge_daemon_started_dev_no_modem" in log:
            ready = True
        if "cloud_edge_stream_opened" in log or "cloud_edge_stream_connected" in log:
            grpc_up = True
        if ready and grpc_up:
            print("  ✓ edge_daemon_started_dev_no_modem + cloud_edge_stream up", file=sys.stderr)
            return proc
        time.sleep(0.5)

    # timeout — show tail
    log = EDGE_LOG_PATH.read_text(errors="replace")[-2000:] if EDGE_LOG_PATH.exists() else ""
    sys.exit(
        f"  ✗ edge daemon ready timeout (ready={ready}, grpc_up={grpc_up}); "
        f"tail of {EDGE_LOG_PATH}:\n{log}"
    )


REMOTE_INJECT_SOURCE = '''
"""Run on ECS: build DialRequest + LPUSH engine:dial. No PG writes."""
import asyncio, json, sys
from isales_common.schemas.messages.dial import (
    DialRequest, DialLeadInfo, PromptVersionsSnapshot,
)
from redis.asyncio import Redis

LEAD_ID = int(sys.argv[1])
CAMPAIGN_ID = int(sys.argv[2])
PHONE = sys.argv[3]
CALLER_ID = sys.argv[4]
DEVICE_ID = int(sys.argv[5])
REDIS_URL = sys.argv[6]

async def main():
    req = DialRequest(
        lead=DialLeadInfo(lead_id=LEAD_ID, campaign_id=CAMPAIGN_ID, phone=PHONE),
        history=[],
        prompt_versions=PromptVersionsSnapshot(),
        caller_id=CALLER_ID,
        device_id=DEVICE_ID,
    )
    r = Redis.from_url(REDIS_URL)
    try:
        await r.incr("isales:concurrency:active")
        await r.lpush("engine:dial", req.model_dump_json())
    finally:
        await r.aclose()
    print(json.dumps({
        "ok": True,
        "message_id": str(req.message_id),
        "lead_id": LEAD_ID,
        "phone": PHONE,
    }))

asyncio.run(main())
'''


def phase_inject_dial(args: argparse.Namespace) -> dict[str, Any]:
    """scp remote injector + ssh exec → LPUSH engine:dial."""
    print("[4/6] inject DialRequest via Redis LPUSH engine:dial ...", file=sys.stderr)

    local_inject = Path("/tmp/_remote_inject_dial.py")
    local_inject.write_text(REMOTE_INJECT_SOURCE)
    scp_to(args.ssh_host, args.ssh_key, local_inject, REMOTE_INJECT_PATH)

    remote = (
        f"{ECS_VENV_PY} {REMOTE_INJECT_PATH} "
        f"{args.lead_id} {args.campaign_id} {shlex.quote(args.phone)} "
        f"{shlex.quote(args.caller_id)} {args.device_id} "
        f"{shlex.quote(ECS_REDIS_URL)}"
    )
    out = ssh_cmd(args.ssh_host, args.ssh_key, remote)
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        sys.exit(f"  ✗ unexpected injector output: {out}")
    if not result.get("ok"):
        sys.exit(f"  ✗ inject failed: {result}")
    print(f"  ✓ injected message_id={result['message_id']} lead={result['lead_id']} phone={result['phone']}", file=sys.stderr)
    return result


def phase_poll_call_record(args: argparse.Namespace) -> dict[str, Any]:
    """Poll PG call_record for lead_id; loop until ended/failed or timeout."""
    print(f"[5/6] poll call_record (timeout {args.timeout}s) ...", file=sys.stderr)
    deadline = time.time() + args.timeout
    last_status: str | None = None
    last_id: int | None = None

    while time.time() < deadline:
        # column 'status' on call_record
        row = ssh_cmd(
            args.ssh_host, args.ssh_key,
            f"{ECS_PSQL} \"SELECT id, status, started_at, ended_at FROM call_record "
            f"WHERE lead_id={args.lead_id} ORDER BY id DESC LIMIT 1;\""
        )
        if row and "|" in row:
            parts = [p.strip() for p in row.split("|")]
            call_id = int(parts[0])
            status = parts[1]
            if call_id != last_id or status != last_status:
                print(f"  · call_record id={call_id} status={status}", file=sys.stderr)
                last_id, last_status = call_id, status
            # CallStatus terminal values: end / abandoned (see isales-common enums.py)
            if status in {"end", "ended", "failed", "abandoned"}:
                # fetch transcript
                transcript_json = ssh_cmd(
                    args.ssh_host, args.ssh_key,
                    f"{ECS_PSQL} \"SELECT transcript FROM call_record WHERE id={call_id};\""
                )
                try:
                    transcript = json.loads(transcript_json) if transcript_json else []
                except json.JSONDecodeError:
                    transcript = transcript_json
                print(f"  ✓ call_record {call_id} reached terminal status={status}", file=sys.stderr)
                return {"call_id": call_id, "status": status, "transcript": transcript}
        time.sleep(2)

    if last_id is None:
        sys.exit("  ✗ no call_record row appeared — engine may not have consumed engine:dial")
    print(f"  · timed out at call_record id={last_id} status={last_status} (may still be live)", file=sys.stderr)
    # fetch whatever transcript exists right now
    transcript_json = ssh_cmd(
        args.ssh_host, args.ssh_key,
        f"{ECS_PSQL} \"SELECT transcript FROM call_record WHERE id={last_id};\""
    )
    try:
        transcript = json.loads(transcript_json) if transcript_json else []
    except json.JSONDecodeError:
        transcript = transcript_json
    return {"call_id": last_id, "status": last_status or "timeout", "transcript": transcript}


def phase_listen_check(result: dict[str, Any]) -> bool:
    """Interactive yes/no on whether mac speaker actually played AI greeting."""
    print("[6/6] listen check (人耳判断) ...", file=sys.stderr)
    transcript = result.get("transcript")
    if isinstance(transcript, list) and transcript:
        print("\n  transcript snapshot:", file=sys.stderr)
        for i, turn in enumerate(transcript[:8]):
            speaker = turn.get("speaker", "?") if isinstance(turn, dict) else "?"
            text = turn.get("text", str(turn)) if isinstance(turn, dict) else str(turn)
            print(f"    [{i}] {speaker}: {text}", file=sys.stderr)
    else:
        print(f"  transcript empty / non-list: {transcript!r}", file=sys.stderr)
    print(
        "\n  问题: mac 扬声器是否真听到 AI 开场白? (y/n)",
        file=sys.stderr,
    )
    ans = input("  → ").strip().lower()
    return ans in {"y", "yes", "1", "true"}


# ---------- main ------------------------------------------------------------


def build_argparser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mac_dev_no_modem_smoke",
        description=__doc__.splitlines()[0],
    )
    p.add_argument("--ssh-host", default=DEFAULT_SSH_HOST)
    p.add_argument("--ssh-key", default=DEFAULT_SSH_KEY)
    p.add_argument("--edge-device-id", default=DEFAULT_EDGE_DEVICE_ID,
                   help=f"JWT sub claim; must match ECS engine.env::"
                        f"ISALES_ENGINE_EDGE_DEVICE_ID (default {DEFAULT_EDGE_DEVICE_ID})")
    p.add_argument("--ttl", default="1h")
    p.add_argument("--ecs-endpoint", default="121.89.85.150:50051")
    p.add_argument("--rtc-framework", default=DEFAULT_RTC_FRAMEWORK)
    p.add_argument("--campaign-id", type=int, default=DEFAULT_CAMPAIGN_ID)
    p.add_argument("--lead-id", type=int, default=DEFAULT_LEAD_ID)
    p.add_argument("--phone", default=DEFAULT_PHONE)
    p.add_argument("--caller-id", default=DEFAULT_CALLER_ID)
    p.add_argument("--device-id", type=int, default=DEFAULT_DEVICE_ID)
    p.add_argument("--timeout", type=int, default=60,
                   help="call_record poll timeout seconds")
    p.add_argument("--skip-edge-start", action="store_true",
                   help="assume edge daemon already running externally")
    p.add_argument("--no-listen-check", action="store_true",
                   help="CI / non-interactive; skip yes/no prompt")
    p.add_argument("--dry-run", action="store_true",
                   help="print intent + ssh test only")
    p.add_argument("--preflight-only", action="store_true",
                   help="只跑 phase 1 (cloud systemd + DingRTC + PG + provider_credential check) 立即退出; 用于 sanity check, 不 mint JWT 不 spawn edge")
    return p


def main() -> int:
    args = build_argparser().parse_args()

    if args.dry_run:
        print(f"[dry-run] would ssh {args.ssh_host} (key {args.ssh_key})", file=sys.stderr)
        print(f"[dry-run] would mint JWT sub={args.edge_device_id} ttl={args.ttl}", file=sys.stderr)
        print(f"[dry-run] would spawn: {TELEPHONY_EDGE_BIN} --dev-no-modem", file=sys.stderr)
        print(f"[dry-run] would inject DialRequest(lead_id={args.lead_id}, "
              f"campaign_id={args.campaign_id}, phone={args.phone})", file=sys.stderr)
        print(f"[dry-run] would poll call_record for {args.timeout}s", file=sys.stderr)
        return 0

    phase_preflight(args)
    if args.preflight_only:
        print("\n=== preflight OK; --preflight-only set, exiting ===", file=sys.stderr)
        return 0
    jwt = phase_mint_jwt(args)

    edge_proc: subprocess.Popen[bytes] | None = None
    if args.skip_edge_start:
        print("[3/6] skipping edge daemon start (--skip-edge-start)", file=sys.stderr)
    else:
        edge_proc = phase_start_edge(args, jwt)

    try:
        phase_inject_dial(args)
        result = phase_poll_call_record(args)
        listened = True
        if not args.no_listen_check:
            listened = phase_listen_check(result)

        # final summary
        summary = {
            "ok": result["status"] in {"ended"} and listened,
            "call_id": result["call_id"],
            "status": result["status"],
            "transcript_len": len(result["transcript"]) if isinstance(result["transcript"], list) else None,
            "listened": listened,
            "edge_log": str(EDGE_LOG_PATH),
        }
        print("\n=== smoke summary ===", file=sys.stderr)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["ok"] else 1
    finally:
        if edge_proc is not None:
            print(f"\n[cleanup] terminating edge daemon pid={edge_proc.pid}", file=sys.stderr)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(os.getpgid(edge_proc.pid), signal.SIGTERM)
            with contextlib.suppress(subprocess.TimeoutExpired):
                edge_proc.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
