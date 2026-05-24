"""pybind §9.5 真 PCM push/pull loopback smoke (Windows edge).

Spec: joint-mvp-gate-13301035545 § 4；windows-artc-pybind11 §9.5。

Mint token via ECS CLI → ARTC EngineHandle join 同 channel → push 5s silence
PCM → 等接收至少 1 inbound frame (ECS 端正在推 silence) → leave。

**启动顺序**: ECS 端**先**跑 ``ecs_pcm_loopback_listen.py``，本脚本**后**
跑（相隔 ≤ 30s）。

通过条件: 5s 内至少 1 inbound PCM frame；本 change 不卡 P95 latency
(那是 §9.5 后续 dial-quality change 的事)。

Usage::

    py -3.12 scripts/pybind_pcm_loopback_smoke.py \\
        --channel smoke-channel-9-5 --user-id edge-smoke --duration 5
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from dataclasses import dataclass

from pybind_rtc_join_smoke import (  # type: ignore[import-not-found]
    DEFAULT_SSH_HOST,
    DEFAULT_SSH_KEY,
    mint_token_via_ssh,
)


@dataclass
class LoopbackResult:
    inbound_frames: int = 0
    inbound_bytes: int = 0
    first_frame_at: float | None = None
    error_events: list[str] | None = None

    def __post_init__(self) -> None:
        if self.error_events is None:
            self.error_events = []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="pybind §9.5 真 PCM loopback smoke")
    parser.add_argument("--channel", default="smoke-channel-9-5")
    parser.add_argument("--user-id", dest="user_id", default="edge-smoke")
    parser.add_argument("--duration", type=int, default=5)
    parser.add_argument("--silence-chunk-ms", type=int, default=100)
    parser.add_argument("--ssh-host", dest="ssh_host", default=DEFAULT_SSH_HOST)
    parser.add_argument("--ssh-key", dest="ssh_key", default=DEFAULT_SSH_KEY)
    args = parser.parse_args(argv)

    print(f"[1/5] minting token (ssh {args.ssh_host}) ...", file=sys.stderr)
    creds = mint_token_via_ssh(
        ssh_host=args.ssh_host,
        ssh_key=args.ssh_key,
        channel=args.channel,
        user_id=args.user_id,
        ttl=args.duration + 600,
    )

    try:
        import aliyun_artc_pywrap as artc  # type: ignore[import-not-found]
    except ImportError as e:
        sys.exit(f"error: aliyun_artc_pywrap import failed: {e}")

    print("[2/5] creating EngineHandle ...", file=sys.stderr)
    engine = artc.EngineHandle.create(creds["app_id"])

    result = LoopbackResult()
    join_event = threading.Event()
    push_started_at = 0.0

    class Listener:
        def on_join_channel_result(self, code: int, channel: str) -> None:
            print(
                f"      on_join_channel_result(code={code}, channel={channel!r})",
                file=sys.stderr,
            )
            if code == 0:
                join_event.set()

        def on_audio_frame(self, uid: str, pcm: bytes) -> None:  # vendor-specific
            result.inbound_frames += 1
            result.inbound_bytes += len(pcm)
            if result.first_frame_at is None:
                result.first_frame_at = time.time()
                print(
                    f"      first inbound frame from uid={uid!r} "
                    f"@ t+{result.first_frame_at - push_started_at:.2f}s "
                    f"({len(pcm)} bytes)",
                    file=sys.stderr,
                )

        def on_error(self, code: int, msg: str) -> None:
            assert result.error_events is not None
            result.error_events.append(f"code={code} msg={msg}")
            print(f"      on_error: code={code} msg={msg!r}", file=sys.stderr)

    listener = Listener()
    engine.set_listener(listener)

    print(
        f"[3/5] join channel={args.channel!r} as uid={args.user_id!r} ...",
        file=sys.stderr,
    )
    engine.join_channel(creds["token"], args.channel, args.user_id)
    if not join_event.wait(timeout=5.0):
        engine.leave_channel()
        engine.destroy()
        sys.exit("error: join_channel timeout — see §9.4 troubleshooting")

    print(f"[4/5] pushing {args.duration}s silence + waiting inbound ...", file=sys.stderr)
    chunk_samples = int(8000 * args.silence_chunk_ms / 1000)
    silence_pcm = b"\x00\x00" * chunk_samples
    push_started_at = time.time()
    elapsed = 0.0
    push_count = 0
    while elapsed < args.duration:
        try:
            # vendor pybind 暴露的 push API；具体方法名可能因 SDK 版本而异，
            # 调试时若失败，参考 aliyun_artc_pywrap.dir() 6 个公开符号。
            engine.push_audio_frame(silence_pcm, push_count * args.silence_chunk_ms)
        except Exception as e:  # noqa: BLE001
            print(f"      push_audio_frame error: {e}", file=sys.stderr)
            break
        push_count += 1
        elapsed = time.time() - push_started_at
        time.sleep(args.silence_chunk_ms / 1000.0)

    print("[5/5] leaving ...", file=sys.stderr)
    engine.leave_channel()
    engine.destroy()

    assert result.error_events is not None
    payload = {
        "ok": result.inbound_frames > 0 and not result.error_events,
        "channel": args.channel,
        "user_id": args.user_id,
        "push_count": push_count,
        "inbound_frames": result.inbound_frames,
        "inbound_bytes": result.inbound_bytes,
        "first_inbound_frame_at_s": (
            result.first_frame_at - push_started_at if result.first_frame_at else None
        ),
        "error_events": result.error_events,
    }
    print(json.dumps(payload))
    if not payload["ok"]:
        print("✗ §9.5 PCM loopback FAILED", file=sys.stderr)
        return 1
    print("✓ §9.5 PCM loopback PASSED", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
