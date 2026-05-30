r"""Windows DingRTC high-level Session smoke — push_audio + drain inbound.

Spec: openspec/changes/engine-rtc-dingrtc-migration § 7.7
      (push_audio + drain_inbound_frames 实测).

Layer differs from `scripts/windows_dingrtc_smoke.py` (§ 7.5):

  * §7.5 smoke calls dingrtc_pywrap.EngineHandle directly — isolates
    binding + token + SDK join itself.
  * §7.7 smoke (this file) uses the high-level
    :class:`WindowsDingRtcSession` (`§ 7.6` wrapper) — exercises the
    full async path: callback marshaling, asyncio.Queue drainer, 10 ms
    push_audio slicing + pacing, clean teardown.

If §7.5 passed but §7.7 fails, the bug is in the wrapper layer
(callback marshaling, push slicing, or drainer logic), not in the
binding or SDK.

Usage::

    .\.venv-3.12\Scripts\python.exe scripts\windows_dingrtc_session_smoke.py
    .\.venv-3.12\Scripts\python.exe scripts\windows_dingrtc_session_smoke.py --push-seconds 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse
import urllib.request

DEMO_APPID = "a4zfr1hn"
DEMO_APP_SERVER = "https://onertc-demo-app-server.dingtalk.com/login"
DEMO_PASSWD = "hello1234"
DEFAULT_GSLB = "https://gslb.dingrtc.com"

# ---------------------------------------------------------------------------
# DLL + .pyd path wiring (must happen BEFORE importing windows_dingrtc_session,
# which transitively imports dingrtc_pywrap at module load).
# ---------------------------------------------------------------------------

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDOR_ROOT = os.environ.get(
    "ISALES_DINGRTC_WINDOWS_SDK_PATH",
    os.path.join(
        os.environ.get("USERPROFILE", ""),
        "codes",
        "vendor",
        "DingRTC_Windows_SDK_3_9_0",
    ),
)
_VENDOR_DLL_DIR = os.path.join(_VENDOR_ROOT, "lib", "x64")
_PYD_DIR = os.path.join(_REPO_ROOT, "build", "dingrtc-binding", "Release")

if not os.path.isdir(_VENDOR_DLL_DIR):
    raise SystemExit(
        f"DingRTC vendor DLL dir not found: {_VENDOR_DLL_DIR}"
    )
if not os.path.isdir(_PYD_DIR):
    raise SystemExit(
        f"dingrtc_pywrap build dir not found: {_PYD_DIR}. Build first."
    )

os.add_dll_directory(_VENDOR_DLL_DIR)
if _PYD_DIR not in sys.path:
    sys.path.insert(0, _PYD_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# Now import the wrapper.
from isales_telephony.audio_bridge.windows_dingrtc_session import (  # noqa: E402
    WindowsDingRtcSession,
)


def fetch_demo_token(channel: str, user_id: str) -> tuple[str, list[str]]:
    """Mirror of windows_dingrtc_smoke.py::fetch_demo_token."""
    params = {
        "passwd": DEMO_PASSWD,
        "env": "onertcOnline",
        "appid": DEMO_APPID,
        "userid": user_id,
        "user": user_id,
        "room": channel,
        "tokensid": "false",
    }
    url = f"{DEMO_APP_SERVER}?{urllib.parse.urlencode(params)}"
    print(f"[1/5] GET demo-app-server: {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    parsed = json.loads(body)
    data = parsed.get("data") or parsed
    token = data.get("token", "")
    gslbs = data.get("gslb") or []
    if not token:
        raise SystemExit(f"demo-app-server returned no token; body={body!r}")
    print(
        f"[1/5]   token len={len(token)} gslb_count={len(gslbs)}",
        file=sys.stderr,
    )
    return token, gslbs


async def push_silence(
    session: WindowsDingRtcSession,
    seconds: float,
    sample_rate: int,
) -> int:
    """Push int16 mono silence for ``seconds``, in 100ms chunks (10 frames each).

    Returns the total byte count pushed. Each chunk is sized to 100ms at
    the negotiated send_sample_rate — the wrapper itself slices to 10ms
    grid internally before handing to the binding.
    """
    chunk_ms = 100
    chunk_bytes = (sample_rate // 1000) * chunk_ms * 2  # int16 mono
    silence = b"\x00" * chunk_bytes
    n_chunks = max(1, int(seconds * 1000 / chunk_ms))
    total = 0
    base_ts = int(time.time() * 1000)
    for i in range(n_chunks):
        await session.push_audio(silence, timestamp_ms=base_ts + i * chunk_ms)
        total += chunk_bytes
    return total


async def drain_concurrent(
    session: WindowsDingRtcSession,
    seconds: float,
) -> dict:
    """Drain inbound frames for ``seconds`` and report counts.

    Demo path single-peer expects some frames from SDK mixer (PLAYBACK
    position includes self-loopback baseline; cloud doc notes RMS
    ~600-800 with silence pushed). Zero frames is *not* a hard fail —
    demo channel behavior is vendor-dependent — but logged.
    """
    stats = {"frames": 0, "total_bytes": 0, "first_pcm_len": 0, "last_ts": 0}
    deadline = asyncio.get_event_loop().time() + seconds

    async def _consume() -> None:
        async for frame in session.audio_frames():
            stats["frames"] += 1
            stats["total_bytes"] += len(frame.pcm)
            if stats["first_pcm_len"] == 0:
                stats["first_pcm_len"] = len(frame.pcm)
            stats["last_ts"] = frame.timestamp_ms
            if asyncio.get_event_loop().time() >= deadline:
                return

    try:
        await asyncio.wait_for(_consume(), timeout=seconds + 1.0)
    except asyncio.TimeoutError:
        pass
    return stats


async def run(args: argparse.Namespace) -> int:
    channel = args.channel or f"win-sess-smoke-{int(time.time())}"
    user_id = args.user_id

    token, gslbs = fetch_demo_token(channel, user_id)
    gslb_server = gslbs[0] if gslbs else DEFAULT_GSLB

    print(
        f"[2/5] constructing WindowsDingRtcSession "
        f"(app_id={DEMO_APPID}, gslb={gslb_server})",
        file=sys.stderr,
    )
    session = WindowsDingRtcSession(app_id=DEMO_APPID, gslb_server=gslb_server)

    print(f"[3/5] join channel={channel!r} as uid={user_id!r} ...", file=sys.stderr)
    t0 = time.monotonic()
    try:
        await session.join(
            channel, token, user_id,
            send_sample_rate=args.sample_rate,
            send_channels=1,
        )
    except Exception as exc:
        print(f"[FAIL] join failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    join_elapsed_ms = (time.monotonic() - t0) * 1000
    print(
        f"[3/5]   joined ✓ is_joined={session.is_joined} "
        f"(wall_elapsed={join_elapsed_ms:.0f}ms)",
        file=sys.stderr,
    )

    # Concurrent push + drain.
    print(
        f"[4/5] push silence {args.push_seconds}s + drain inbound {args.drain_seconds}s "
        f"(concurrent) ...",
        file=sys.stderr,
    )
    push_task = asyncio.create_task(
        push_silence(session, args.push_seconds, args.sample_rate),
        name="push-silence",
    )
    drain_task = asyncio.create_task(
        drain_concurrent(session, args.drain_seconds),
        name="drain-inbound",
    )

    try:
        pushed_bytes, drain_stats = await asyncio.gather(push_task, drain_task)
    except Exception as exc:
        print(f"[FAIL] push/drain raised: {type(exc).__name__}: {exc}", file=sys.stderr)
        await session.leave()
        return 3

    print(
        f"[4/5]   pushed {pushed_bytes} bytes "
        f"({pushed_bytes / (args.sample_rate * 2):.2f}s @ {args.sample_rate}Hz mono int16)",
        file=sys.stderr,
    )
    print(
        f"[4/5]   drained {drain_stats['frames']} inbound frames "
        f"({drain_stats['total_bytes']} bytes; "
        f"first_pcm_len={drain_stats['first_pcm_len']}; "
        f"last_ts={drain_stats['last_ts']})",
        file=sys.stderr,
    )

    print(f"[5/5] leaving cleanly", file=sys.stderr)
    try:
        await session.leave()
    except Exception as exc:
        print(f"[FAIL] leave raised: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 4

    print(
        f"[5/5] PASS — joined, pushed {pushed_bytes}B, "
        f"drained {drain_stats['frames']} frames, left cleanly.",
        file=sys.stderr,
    )
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="", help="channel (default: win-sess-smoke-<ts>)")
    ap.add_argument("--user-id", default="win-sess-smoke")
    ap.add_argument("--push-seconds", type=float, default=2.0, help="push silence duration (default: 2s)")
    ap.add_argument("--drain-seconds", type=float, default=3.0, help="drain duration (default: 3s)")
    ap.add_argument("--sample-rate", type=int, default=16000)
    args = ap.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
