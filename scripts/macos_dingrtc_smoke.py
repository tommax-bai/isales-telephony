"""mac dev DingRTC PyObjC binding smoke.

Spec: openspec/changes/engine-rtc-dingrtc-migration § 8.8.

Verifies the macOS PyObjC binding end-to-end against the vendor demo
app server + demo appid ``a4zfr1hn``:

1. Fetch a server-issued token from
   ``https://onertc-demo-app-server.dingtalk.com/login`` (vendor demo
   path; no client-side AppKey needed, no self-sign).
2. Build ``MacosDingRtcPyObjCSession`` against the real
   ``DingRTC.framework`` on disk.
3. ``join(channel, token, uid)`` → MUST see
   ``onJoinChannelResult:channel:userId:elapsed:`` fire with ``result=0``.
4. Wait a few seconds (so the SDK observer queue can be inspected) +
   leave cleanly.

Usage::

    .venv/bin/python scripts/macos_dingrtc_smoke.py
    .venv/bin/python scripts/macos_dingrtc_smoke.py --real    # real AppId path
    .venv/bin/python scripts/macos_dingrtc_smoke.py --channel my-channel

For the ``--real`` path the script expects ``ISALES_RTC_APP_ID`` +
``ISALES_RTC_APP_KEY`` env and uses the same self-sign token shape as
``isales-engine/scripts/dingrtc_demo_smoke.py``. Run that variant
*after* the demo path passes, so failures don't conflate "binding
wiring wrong" with "credentials wrong".
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

# Allow running from repo root without installing the package.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from isales_telephony.audio_bridge.macos_dingrtc_pyobjc import (  # noqa: E402
    MacosDingRtcPyObjCSession,
)

DEMO_APPID = "a4zfr1hn"
DEMO_APP_SERVER = "https://onertc-demo-app-server.dingtalk.com/login"
DEMO_PASSWD = "hello1234"


def fetch_demo_token(channel: str, user_id: str) -> tuple[str, list[str]]:
    """Hit the vendor demo-app-server to mint a server-issued token.

    Returns ``(token, gslb_servers)``. The vendor response shape::

        {"token": "<jwt-like>", "gslb": ["https://...", ...], ...}
    """
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
    print(f"[1/4] GET demo-app-server: {url}", file=sys.stderr)
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = resp.read().decode("utf-8")
    print(f"[1/4]   raw response (truncated): {body[:200]}", file=sys.stderr)
    parsed = json.loads(body)
    # vendor envelope: {"code": 0, "data": {"token": ..., "gslb": [...]}}
    data = parsed.get("data") or parsed
    token = data.get("token", "")
    gslbs = data.get("gslb") or []
    if not token:
        raise SystemExit(f"demo-app-server returned no token; body={body!r}")
    print(
        f"[1/4]   token len={len(token)} gslb_count={len(gslbs)}",
        file=sys.stderr,
    )
    return token, gslbs


def sign_real_token(*, channel: str, user_id: str, ttl_seconds: int) -> str:
    """Sign a real-AppId token via the isales-engine token issuer.

    Falls back to importing ``isales_engine.transport.rtc_token`` from a
    sibling ``../isales-engine`` checkout — this mirrors the dev mac
    layout (`~/codes/isales-telephony` + `~/codes/isales-engine`).
    """
    engine_root = os.path.abspath(os.path.join(_REPO_ROOT, "..", "isales-engine"))
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    try:
        from isales_engine.transport.rtc_token import RtcTokenIssuer  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            f"--real requires sibling isales-engine checkout at {engine_root}: {exc}",
        ) from exc

    app_id = os.environ.get("ISALES_RTC_APP_ID", "")
    app_key = os.environ.get("ISALES_RTC_APP_KEY", "")
    if not app_id or not app_key:
        raise SystemExit("--real requires ISALES_RTC_APP_ID + ISALES_RTC_APP_KEY env")

    issuer = RtcTokenIssuer(app_id=app_id, app_key=app_key)
    creds = issuer.sign(
        channel=channel, user_id=user_id, ttl_seconds=ttl_seconds,
    )
    return creds.token


async def run(args: argparse.Namespace) -> dict:
    channel = args.channel or f"mac-smoke-{int(time.time())}"
    user_id = args.user_id

    if args.real:
        app_id = os.environ.get("ISALES_RTC_APP_ID", "")
        if not app_id:
            raise SystemExit("--real requires ISALES_RTC_APP_ID env")
        token = sign_real_token(
            channel=channel, user_id=user_id, ttl_seconds=int(args.duration) + 600,
        )
        gslbs: list[str] = []  # use default gslb.dingrtc.com
    else:
        app_id = DEMO_APPID
        token, gslbs = fetch_demo_token(channel, user_id)

    print(
        f"[2/4] constructing MacosDingRtcPyObjCSession (app_id={app_id})",
        file=sys.stderr,
    )
    session_kwargs: dict = {"app_id": app_id}
    if gslbs:
        # Vendor demo path uses a non-default GSLB; honour the first
        # entry the demo-app-server returned.
        session_kwargs["gslb_server"] = gslbs[0]
    session = MacosDingRtcPyObjCSession(**session_kwargs)

    print(
        f"[3/4] join channel={channel!r} as uid={user_id!r} ...",
        file=sys.stderr,
    )
    sample_rate = args.sample_rate
    await session.join(
        channel, token, user_id,
        send_sample_rate=sample_rate, send_channels=1,
    )
    print(f"[3/4]   joined; is_joined={session.is_joined}", file=sys.stderr)

    push_count = 0
    inbound_frames = 0
    inbound_bytes = 0
    first_inbound_at: float | None = None
    started = time.monotonic()
    push_task: asyncio.Task | None = None
    drain_task: asyncio.Task | None = None

    # Silence-push task — mirrors ECS scripts/ecs_pcm_loopback_listen.py so a
    # dual-peer test produces symmetric inbound counters on both sides.
    # 100 ms × sample_rate × 16-bit mono.
    if args.push_silence:
        silence_chunk_ms = 100
        chunk_samples = int(sample_rate * silence_chunk_ms / 1000)
        silence_pcm = b"\x00\x00" * chunk_samples

        async def _push_loop() -> None:
            nonlocal push_count
            ts = 0
            while True:
                try:
                    await session.push_audio(silence_pcm, timestamp_ms=ts)
                except Exception as exc:  # noqa: BLE001
                    print(f"      push error: {exc}", file=sys.stderr)
                    return
                push_count += 1
                ts += silence_chunk_ms
                await asyncio.sleep(silence_chunk_ms / 1000)

        push_task = asyncio.create_task(_push_loop())

    if args.drain:
        async def _drain_loop() -> None:
            nonlocal inbound_frames, inbound_bytes, first_inbound_at
            async for frame in session.audio_frames():
                inbound_frames += 1
                inbound_bytes += len(frame.pcm)
                if first_inbound_at is None:
                    first_inbound_at = time.monotonic()
                    print(
                        f"      first inbound frame at t+"
                        f"{first_inbound_at - started:.2f}s "
                        f"({len(frame.pcm)} bytes)",
                        file=sys.stderr,
                    )

        drain_task = asyncio.create_task(_drain_loop())

    # Hold the channel long enough for the audio observer to confirm
    # the inbound wire is live. Single-peer (no flags) keeps the
    # original §8.8 minimal-join behaviour; with --push-silence + --drain
    # mirrors the ECS counter pattern for dual-peer evidence.
    await asyncio.sleep(args.duration)

    import contextlib  # local — only needed when tasks were spawned
    for t in (push_task, drain_task):
        if t is not None:
            t.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    print(f"[4/4] leaving ...", file=sys.stderr)
    await session.leave()
    return {
        "ok": True,
        "channel": channel,
        "app_id": app_id,
        "duration_s": args.duration,
        "real_mode": args.real,
        "push_count": push_count,
        "inbound_frames": inbound_frames,
        "inbound_bytes": inbound_bytes,
        "first_inbound_frame_at_s": (
            first_inbound_at - started if first_inbound_at else None
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="mac dev DingRTC PyObjC binding smoke (§8.8)",
    )
    parser.add_argument("--channel", default=None)
    parser.add_argument("--user-id", dest="user_id", default="mac-smoke-01")
    parser.add_argument(
        "--duration", type=float, default=3.0,
        help="hold the channel this many seconds after join",
    )
    parser.add_argument(
        "--sample-rate", dest="sample_rate", type=int, default=8000,
    )
    parser.add_argument(
        "--real", action="store_true",
        help="use real AppId + self-signed token (ISALES_RTC_APP_ID/KEY env)",
    )
    parser.add_argument(
        "--push-silence", dest="push_silence", action="store_true",
        help="push 100ms silence PCM chunks for the duration (dual-peer test)",
    )
    parser.add_argument(
        "--drain", action="store_true",
        help="consume audio_frames() and report inbound counters",
    )
    args = parser.parse_args(argv)

    try:
        stats = asyncio.run(run(args))
    except Exception as exc:  # noqa: BLE001
        print(f"smoke FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(stats))
    return 0


if __name__ == "__main__":
    sys.exit(main())
