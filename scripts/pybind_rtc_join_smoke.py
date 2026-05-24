"""pybind §9.4 真 RTC join smoke (Windows edge).

Spec: joint-mvp-gate-13301035545 § 3；windows-artc-pybind11 §9.4。

Mint token via ECS CLI (`isales-engine-mint-rtc-token`) → ARTC
``EngineHandle.create()`` → ``join_channel(token, channel)`` → 等
``on_join_channel_result(code=0)`` 事件回调 → idle ``--duration`` 秒 →
``leave_channel()`` → ``destroy()``。

通过条件:
- 5 秒内出现 ``on_join_channel_result`` 且 code == 0
- idle 期间无 disconnect / error event
- clean leave 不抛异常

Usage::

    py -3.12 scripts/pybind_rtc_join_smoke.py \\
        --channel smoke-channel-9-4 \\
        --user-id edge-smoke \\
        --duration 5 \\
        --ssh-host root@121.89.85.150 \\
        --ssh-key C:/Users/tianx/codes/isales.pem

Exits 0 = pass，非 0 = 不通过；STATE.md 写证据前必须 0。
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field

DEFAULT_SSH_HOST = "root@121.89.85.150"
DEFAULT_SSH_KEY = "C:/Users/tianx/codes/isales.pem"

# CLI 路径 ssh 跑：sudo -u isales -H -E env $(cat engine.env | xargs)
# /opt/isales/current/venv/bin/isales-engine-mint-rtc-token --channel X
# --user-id Y --ttl 600
SSH_REMOTE_CMD_TEMPLATE = (
    "sudo -u isales -H -E env $(cat /etc/isales/env/engine.env | "
    "grep -v '^#' | grep -v '^$' | xargs) "
    "/opt/isales/current/venv/bin/isales-engine-mint-rtc-token "
    "--channel {channel} --user-id {user_id} --ttl {ttl}"
)


@dataclass
class JoinResult:
    code: int | None = None
    join_event_at: float | None = None
    error_events: list[str] = field(default_factory=list)


def mint_token_via_ssh(
    *, ssh_host: str, ssh_key: str, channel: str, user_id: str, ttl: int
) -> dict:
    """ssh 到 ECS 跑 isales-engine-mint-rtc-token；返回 JSON dict。"""
    remote_cmd = SSH_REMOTE_CMD_TEMPLATE.format(
        channel=shlex.quote(channel),
        user_id=shlex.quote(user_id),
        ttl=ttl,
    )
    cmd = [
        "ssh", "-i", ssh_key, "-o", "ConnectTimeout=15", ssh_host, remote_cmd,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        sys.exit(
            f"error: mint-rtc-token ssh failed (rc={proc.returncode}):\n"
            f"stderr: {proc.stderr}"
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        sys.exit(f"error: mint-rtc-token returned non-JSON: {proc.stdout!r}; {e}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pybind §9.4 真 RTC join smoke")
    parser.add_argument("--channel", default="smoke-channel-9-4")
    parser.add_argument("--user-id", dest="user_id", default="edge-smoke")
    parser.add_argument("--duration", type=int, default=5, help="idle 秒数")
    parser.add_argument("--ttl", type=int, default=600)
    parser.add_argument("--ssh-host", dest="ssh_host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--ssh-key", dest="ssh_key", default=DEFAULT_SSH_KEY)
    args = parser.parse_args(argv)

    print(f"[1/4] minting token (ssh {args.ssh_host}) ...", file=sys.stderr)
    creds = mint_token_via_ssh(
        ssh_host=args.ssh_host,
        ssh_key=args.ssh_key,
        channel=args.channel,
        user_id=args.user_id,
        ttl=args.ttl,
    )
    print(
        f"      token minted; channel={creds['channel']} "
        f"app_id={creds['app_id']} expires_at={creds['expires_at']}",
        file=sys.stderr,
    )

    # Import 延后: 仅 ARTC pybind .pyd 在 path 上才能进；提前 import 错误
    # 信息不会带 mint context.
    try:
        import aliyun_artc_pywrap as artc  # type: ignore[import-not-found]
    except ImportError as e:
        sys.exit(
            f"error: aliyun_artc_pywrap import failed: {e}\n"
            "提示: 确认 .pyd 在 PYTHONPATH 或 sys.path；vendor DLL 在 .pyd "
            "同目录或 PATH。详见 isales-telephony/deploy/edge/windows/STATE.md "
            "§ \"pybind11 binding build\"."
        )

    print("[2/4] creating EngineHandle ...", file=sys.stderr)
    engine = artc.EngineHandle.create(creds["app_id"])

    result = JoinResult()
    join_event = threading.Event()

    class Listener:
        def on_join_channel_result(self, code: int, channel: str) -> None:  # noqa: D401
            result.code = code
            result.join_event_at = time.time()
            join_event.set()
            print(
                f"      on_join_channel_result(code={code}, channel={channel!r})",
                file=sys.stderr,
            )

        def on_error(self, code: int, msg: str) -> None:
            result.error_events.append(f"code={code} msg={msg}")
            print(f"      on_error: code={code} msg={msg!r}", file=sys.stderr)

    listener = Listener()
    engine.set_listener(listener)

    print(
        f"[3/4] join_channel(channel={args.channel!r}, user_id={args.user_id!r}) ...",
        file=sys.stderr,
    )
    t0 = time.time()
    engine.join_channel(creds["token"], args.channel, args.user_id)

    if not join_event.wait(timeout=5.0):
        engine.leave_channel()
        engine.destroy()
        sys.exit(
            "error: on_join_channel_result not received in 5s — channel join "
            "failed. 检查: ECS engine ARTC AppKey / 网络可达 / vendor SDK 版本 / "
            "AppId 一致性"
        )

    elapsed = (result.join_event_at or time.time()) - t0
    print(f"      joined in {elapsed*1000:.0f} ms", file=sys.stderr)

    if result.code != 0:
        engine.leave_channel()
        engine.destroy()
        sys.exit(f"error: on_join_channel_result code != 0: code={result.code}")

    print(f"[4/4] idle {args.duration}s 监听 error events ...", file=sys.stderr)
    time.sleep(args.duration)

    if result.error_events:
        sys.exit(
            f"error: {len(result.error_events)} error events during idle: "
            f"{result.error_events}"
        )

    engine.leave_channel()
    engine.destroy()
    print("✓ §9.4 RTC join smoke PASSED", file=sys.stderr)
    print(
        json.dumps(
            {
                "ok": True,
                "channel": args.channel,
                "user_id": args.user_id,
                "join_latency_ms": int(elapsed * 1000),
                "duration_s": args.duration,
                "error_events": result.error_events,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
