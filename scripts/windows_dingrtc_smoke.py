r"""Windows DingRTC pybind11 binding smoke — isolated SDK / binding / token check.

Spec: openspec/changes/engine-rtc-dingrtc-migration § 7.5 (join smoke).

Mirrors `isales-telephony/scripts/macos_dingrtc_smoke.py` (mac PyObjC binding)
and `isales-engine/scripts/dingrtc_demo_smoke.py` (cloud Linux pybind11
binding), targeting the **Windows** pybind11 binding at
`deploy/edge/windows/pybind/dingrtc_pywrap/`.

Verifies in one pass:
1. dingrtc_pywrap extension loads (DLL co-location works);
2. Vendor demo-app-server returns a server-issued token for demo appid
   ``a4zfr1hn`` (no client-side AppKey involved);
3. EngineHandle.join_channel() resolves with OnJoinChannelResult(rc=0)
   within the timeout;
4. clean leave + destroy.

If any of the above fails, the SDK/binding/token isolation cleanly says
which layer to blame:

  * Binding load fails → DLL paths / runtime DLL co-location wrong.
  * Demo-app-server 4xx → vendor URL or appid wrong.
  * on_join_channel_result rc != 0 → token format wrong OR vendor
    rejected this demo's roomserver (rare; should be 0 with documented
    flow).
  * join callback never fires → SDK silently failing or wrong
    listener wiring.

Usage:

    .\.venv-3.12\Scripts\python.exe scripts\windows_dingrtc_smoke.py
    .\.venv-3.12\Scripts\python.exe scripts\windows_dingrtc_smoke.py --duration 10

For the --real path (real AppId + AppKey self-sign):

    $env:ISALES_RTC_APP_ID = "o6dpsan9..."
    $env:ISALES_RTC_APP_KEY = "<real key>"
    .\.venv-3.12\Scripts\python.exe scripts\windows_dingrtc_smoke.py --real \
        --channel rtc-smoke-1234567890

Real-path requires a sibling `../isales-engine` checkout (for
RtcTokenIssuer). Run --real *after* demo path passes — keeps SDK /
binding failures separate from credentials failures.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

DEMO_APPID = "a4zfr1hn"
DEMO_APP_SERVER = "https://onertc-demo-app-server.dingtalk.com/login"
DEMO_PASSWD = "hello1234"  # vendor public demo password
DEFAULT_GSLB = "https://gslb.dingrtc.com"

# Allow running from repo root without installing.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# DLL search + binding import.
# ---------------------------------------------------------------------------

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
        f"DingRTC vendor DLL dir not found: {_VENDOR_DLL_DIR}. "
        "Set ISALES_DINGRTC_WINDOWS_SDK_PATH or extract DingRTC_Windows_SDK_3_9_0.zip "
        f"to %USERPROFILE%/codes/vendor/."
    )
if not os.path.isdir(_PYD_DIR):
    raise SystemExit(
        f"dingrtc_pywrap build output dir not found: {_PYD_DIR}. "
        "Build first: cmake -S deploy/edge/windows/pybind/dingrtc_pywrap "
        "-B build/dingrtc-binding -DPython3_EXECUTABLE=<path-to-.venv-3.12-python.exe>; "
        "cmake --build build/dingrtc-binding --config Release"
    )

os.add_dll_directory(_VENDOR_DLL_DIR)
if _PYD_DIR not in sys.path:
    sys.path.insert(0, _PYD_DIR)

try:
    import dingrtc_pywrap as b  # noqa: E402
except ImportError as exc:
    raise SystemExit(
        f"failed to import dingrtc_pywrap: {exc}\n"
        f"  - .pyd dir: {_PYD_DIR}\n"
        f"  - vendor DLL dir: {_VENDOR_DLL_DIR}\n"
        "Common causes: missing VC runtime, missing dependent DLL, "
        "wrong Python version (must be 3.12 to match .pyd ABI)."
    ) from exc


# ---------------------------------------------------------------------------
# Token fetch (demo path).
# ---------------------------------------------------------------------------


def fetch_demo_token(channel: str, user_id: str) -> tuple[str, list[str]]:
    """Hit vendor demo-app-server to mint a server-issued token.

    Returns ``(token, gslb_servers)``. Vendor envelope:
        {"code": 0, "data": {"token": "<jwt-like>", "gslb": [...], ...}}
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
    """Sign a real-AppId token via sibling isales-engine's RtcTokenIssuer."""
    engine_root = os.path.abspath(os.path.join(_REPO_ROOT, "..", "isales-engine"))
    if engine_root not in sys.path:
        sys.path.insert(0, engine_root)
    try:
        from isales_engine.transport.rtc_token import RtcTokenIssuer  # type: ignore[import-not-found]
    except ImportError as exc:
        raise SystemExit(
            f"--real requires sibling isales-engine at {engine_root}: {exc}"
        ) from exc

    app_id = os.environ.get("ISALES_RTC_APP_ID", "")
    app_key = os.environ.get("ISALES_RTC_APP_KEY", "")
    if not app_id or not app_key:
        raise SystemExit("--real requires ISALES_RTC_APP_ID + ISALES_RTC_APP_KEY env")

    issuer = RtcTokenIssuer(app_id=app_id, app_key=app_key)
    creds = issuer.sign(channel=channel, user_id=user_id, ttl_seconds=ttl_seconds)
    return creds.token


# ---------------------------------------------------------------------------
# Smoke.
# ---------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    channel = args.channel or f"win-smoke-{int(time.time())}"
    user_id = args.user_id

    if args.real:
        app_id = os.environ.get("ISALES_RTC_APP_ID", "")
        if not app_id:
            raise SystemExit("--real requires ISALES_RTC_APP_ID env")
        token = sign_real_token(
            channel=channel, user_id=user_id, ttl_seconds=int(args.duration) + 600
        )
        gslbs: list[str] = []
    else:
        app_id = DEMO_APPID
        token, gslbs = fetch_demo_token(channel, user_id)

    gslb_server = gslbs[0] if gslbs else DEFAULT_GSLB

    print(
        f"[2/4] constructing engine + listener + ring(64) + observer "
        f"(app_id={app_id}, gslb={gslb_server})",
        file=sys.stderr,
    )

    engine = b.EngineHandle()
    listener = b.EngineListener()
    frame_buffer = b.FrameRingBuffer(64)
    observer = b.AudioObserver(frame_buffer, "")

    # Capture join callback result for polling loop.
    join_state: dict = {"fired": False, "result": None, "channel": None,
                        "user_id": None, "elapsed": None}

    def on_join(result: int, ch: str, uid: str, elapsed: int) -> None:
        print(
            f"[3/4]   on_join_channel_result: rc={result} "
            f"(0x{result & 0xFFFFFFFF:08X}) channel={ch!r} uid={uid!r} "
            f"elapsed={elapsed}ms",
            file=sys.stderr,
        )
        join_state["fired"] = True
        join_state["result"] = result
        join_state["channel"] = ch
        join_state["user_id"] = uid
        join_state["elapsed"] = elapsed

    def on_leave(stats=None) -> None:
        print(f"[leave callback: stats={stats}]", file=sys.stderr)

    def on_error(code: int, msg: str) -> None:
        print(
            f"[error callback: code={code} (0x{code & 0xFFFFFFFF:08X}) msg={msg!r}]",
            file=sys.stderr,
        )

    listener.set_on_join(on_join)
    listener.set_on_leave(on_leave)
    listener.set_on_error(on_error)

    print(
        f"[3/4] wiring engine + joining channel={channel!r} as uid={user_id!r} ...",
        file=sys.stderr,
    )
    engine.create("")
    engine.set_event_listener(listener)
    engine.register_audio_observer(observer)
    engine.enable_audio_frame_observer(True, b.POSITION_PLAYBACK)
    engine.set_external_audio_source(True, args.sample_rate, 1)
    engine.publish_local_audio_stream(True)
    engine.publish_local_video_stream(False)
    engine.subscribe_all_remote_audio_streams(True)
    engine.join_channel(channel, user_id, app_id, token, gslb_server, user_id)

    # Poll for join callback (SDK fires on its own thread; main thread
    # just waits).
    deadline = time.time() + args.timeout
    while not join_state["fired"] and time.time() < deadline:
        time.sleep(0.1)

    if not join_state["fired"]:
        print(
            f"[FAIL] OnJoinChannelResult did not fire within {args.timeout}s",
            file=sys.stderr,
        )
        try:
            engine.leave_channel()
            engine.unregister_audio_observer()
            engine.destroy()
        except Exception as exc:
            print(f"  cleanup error: {exc}", file=sys.stderr)
        return 2

    if join_state["result"] != 0:
        print(
            f"[FAIL] join rc={join_state['result']} "
            f"(0x{join_state['result'] & 0xFFFFFFFF:08X}) != 0",
            file=sys.stderr,
        )
        try:
            engine.leave_channel()
            engine.unregister_audio_observer()
            engine.destroy()
        except Exception as exc:
            print(f"  cleanup error: {exc}", file=sys.stderr)
        return 1

    print(f"[3/4]   joined ✓", file=sys.stderr)
    print(f"[4/4] holding {args.duration}s ...", file=sys.stderr)
    time.sleep(args.duration)

    print(f"[4/4] leaving cleanly", file=sys.stderr)
    try:
        engine.leave_channel()
        engine.unregister_audio_observer()
        engine.destroy()
    except Exception as exc:
        print(f"  leave error: {exc}", file=sys.stderr)
        return 3

    print(
        f"[4/4] PASS — joined {channel!r} for {args.duration}s "
        f"(elapsed_join={join_state['elapsed']}ms), then left cleanly.",
        file=sys.stderr,
    )
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channel", default="", help="channel name (default: win-smoke-<ts>)")
    ap.add_argument("--user-id", default="win-smoke", help="user id (default: win-smoke)")
    ap.add_argument("--duration", type=int, default=5, help="seconds to hold the channel before leaving (default: 5)")
    ap.add_argument("--timeout", type=float, default=10.0, help="join callback timeout in seconds (default: 10)")
    ap.add_argument("--sample-rate", type=int, default=16000, help="external audio source sample rate (default: 16000)")
    ap.add_argument("--real", action="store_true", help="use real AppId + self-signed token instead of demo path")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
