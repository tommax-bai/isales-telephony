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

    # Windows: ARTC vendor DLLs 不在系统 PATH 上时 `import .pyd` 会成功
    # 但 EngineHandle.create() 内调用 C++ AliEngine::Create() 触发 DLL
    # 加载会失败 → process 静默 exit。用 os.add_dll_directory 手动注入。
    import os as _os

    if hasattr(_os, "add_dll_directory"):
        pybind_dir = _os.path.dirname(
            _os.path.abspath(__file__)
        ) + r"\..\deploy\edge\windows\pybind\aliyun_artc_pywrap"
        pybind_dir = _os.path.normpath(pybind_dir)
        if _os.path.isdir(pybind_dir):
            _os.add_dll_directory(pybind_dir)
            print(f"      added DLL dir: {pybind_dir}", file=sys.stderr)
        # Also check pybind PYTHONPATH location (override via env)
        for extra in (_os.environ.get("ARTC_DLL_DIR", ""),):
            if extra and _os.path.isdir(extra):
                _os.add_dll_directory(extra)
                print(f"      added DLL dir: {extra}", file=sys.stderr)

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
    engine = artc.EngineHandle()
    # extras MUST 是 JSON 含 app_id；空字符串会触发 ARTC SDK native 侧
    # 静默 abort (exit code 5, no traceback). Verified 2026-05-24.
    extras_json = json.dumps({"app_id": creds["app_id"]})
    engine.create(extras=extras_json)

    result = JoinResult()
    join_event = threading.Event()

    # binding actual signature (engine_listener.cpp:56):
    #   on_join(result: int, channel: str, user_id: str, elapsed_ms: int)
    def on_join(code: int, channel: str, user_id: str, elapsed_ms: int) -> None:
        result.code = code
        result.join_event_at = time.time()
        join_event.set()
        print(
            f"      on_join(code={code}, channel={channel!r}, "
            f"user_id={user_id!r}, elapsed_ms={elapsed_ms})",
            file=sys.stderr,
        )

    def on_error(error: int, msg: str) -> None:
        result.error_events.append(f"error={error} msg={msg}")
        print(f"      on_error: error={error} msg={msg!r}", file=sys.stderr)

    listener = artc.EngineListener()
    listener.set_on_join(on_join)
    listener.set_on_error(on_error)
    engine.set_event_listener(listener)

    # Channel setup setters (windows-artc-pybind11-join-config) — SHALL
    # 在 join_channel 之前依序调；缺任一会触发 ARTC SDK native rc != 0
    # (2026-05-24 实测 rc=16974081)。
    print("      setting up channel (profile / role / external audio) ...", file=sys.stderr)
    engine.set_channel_profile(artc.AliEngineChannelProfile.ChannelProfileInteractiveLive)
    engine.set_client_role(artc.AliEngineClientRole.AliEngineClientRoleInteractive)
    # external audio stream 8 kHz mono 16-bit — iSales 标准音频格式
    stream_id = engine.add_external_audio_stream(channels=1, sample_rate=8000)
    print(f"      external audio stream_id = {stream_id}", file=sys.stderr)
    engine.publish_local_audio_stream(True)

    print(
        f"[3/4] join_channel(channel={args.channel!r}, user_id={args.user_id!r}) ...",
        file=sys.stderr,
    )
    t0 = time.time()
    try:
        engine.join_channel(creds["token"], args.channel, args.user_id, args.user_id)
    except artc.AliyunArtcError as e:
        # 把真 rc 暴露出来 - AliyunArtcError 有 .code attr，hex 形式更容易查 SDK 错误码表
        rc_val = getattr(e, "code", None) or getattr(e, "args", [None])[0] or "?"
        rc_hex = f"0x{int(rc_val):08X}" if isinstance(rc_val, int) else "?"
        engine.destroy()
        sys.exit(
            f"error: JoinChannel native rc={rc_val} ({rc_hex}); err: {e}\n"
            "ARTC SDK 错误码上 5 字节是 module ID，下 3 字节具体 error:\n"
            "  0x01010xxx: AliEngineErrorJoinChannel* family (e.g.,\n"
            "    0x01010406 PublishNotJoinChannel, 0x01010550 SubscribeNotJoinChannel)\n"
            "  0x01037D81 (16974081): 之前 on_join 异步报的相同 code\n"
            "见 vendor header engine_interface.h:680-720 错误码 enum"
        )

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
