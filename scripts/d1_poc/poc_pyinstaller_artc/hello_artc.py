"""Minimum smoke test program for D1 PoC 2.4.

When run, this script:

1. Imports the Aliyun RTC SDK Windows Python wrapper (path injected via
   ``ISALES_RTC_SDK_WINDOWS_PATH`` env var, or auto-detected inside the
   PyInstaller-frozen onedir layout via ``sys._MEIPASS``).
2. Calls ``CreateAliRTCEngine`` with a dummy config, then immediately
   destroys it.
3. Prints OK + Python+SDK versions and exits 0.

If ARTC SDK Windows isn't on disk yet, the script falls back to
"import-only" mode: it just verifies that PyInstaller's frozen layout
can find the script's own dependencies, prints a NOTICE, and exits 0.
The acceptance criterion for D1 task 2.4 is: BOTH the import-only and
the full SDK-load modes succeed under the frozen exe.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path


def _resolve_sdk_path() -> Path | None:
    """Resolve where the ARTC SDK Windows Python wrapper lives.

    Order:
    1. ``ISALES_RTC_SDK_WINDOWS_PATH`` env var (dev / explicit override).
    2. ``sys._MEIPASS / "vendor/aliyun-artc-windows-python"`` — the
       layout the PyInstaller spec ships (binaries + datas entry).
    3. ``./vendor/aliyun-artc-windows-python`` next to the script (the
       not-frozen developer path).
    """
    env = os.environ.get("ISALES_RTC_SDK_WINDOWS_PATH")
    if env:
        p = Path(env)
        if p.exists():
            return p
    if hasattr(sys, "_MEIPASS"):
        meipass_path = Path(sys._MEIPASS) / "vendor" / "aliyun-artc-windows-python"  # type: ignore[attr-defined]
        if meipass_path.exists():
            return meipass_path
    local = Path(__file__).parent / "vendor" / "aliyun-artc-windows-python"
    if local.exists():
        return local
    return None


def main() -> int:
    result: dict[str, object] = {
        "python": sys.version.split(" ", 1)[0],
        "frozen": getattr(sys, "frozen", False),
        "executable": sys.executable,
        "meipass": getattr(sys, "_MEIPASS", None),
        "sdk_path": None,
        "sdk_imported": False,
        "sdk_engine_created": False,
        "verdict": "",
    }

    sdk_path = _resolve_sdk_path()
    if sdk_path is None:
        result["verdict"] = (
            "PARTIAL — import-only path OK (PyInstaller frozen layout "
            "exercises non-SDK dependencies). Set ISALES_RTC_SDK_WINDOWS_PATH "
            "and rerun to fully validate ARTC DLL loading."
        )
        print(json.dumps(result, indent=2))
        return 0

    result["sdk_path"] = str(sdk_path)
    # Inject the SDK path so the Python wrapper module imports cleanly,
    # AND so the bundled DLLs (next to the .py wrapper) resolve via the
    # process PATH. On Windows, DLL search order includes the directory
    # of the loaded .pyd / .py wrapper.
    sys.path.insert(0, str(sdk_path))
    os.environ["PATH"] = str(sdk_path) + os.pathsep + os.environ.get("PATH", "")

    try:
        import aliyun_rtc_sdk  # type: ignore[import-not-found]  # noqa: PLC0415, F401
    except ImportError:
        # Fall back to common alternative module names — the actual
        # wrapper name from阿里 controller is "AliRTCEngine" / similar;
        # this PoC accepts whichever import works.
        candidates = ["alirtc_sdk", "AliRtcSdk", "aliyun_artc", "alirtc"]
        for name in candidates:
            try:
                mod = __import__(name)
                sys.modules["aliyun_rtc_sdk"] = mod
                break
            except ImportError:
                continue
        else:
            result["verdict"] = (
                "FAIL — SDK path resolved but no recognised wrapper "
                "module name imported. Tried: aliyun_rtc_sdk + "
                f"{candidates}. List of files at SDK path: "
                f"{[p.name for p in sdk_path.iterdir()]}"
            )
            print(json.dumps(result, indent=2))
            return 1

    result["sdk_imported"] = True

    # Try to create + destroy an engine. The exact factory name varies
    # by SDK version; the PoC tolerates the common shapes.
    import aliyun_rtc_sdk as sdk  # type: ignore[import-not-found]  # noqa: PLC0415

    factory_names = [
        "CreateAliRTCEngine",
        "createAliRtcEngine",
        "create_engine",
        "AliRtcEngine",
    ]
    factory = next((getattr(sdk, n) for n in factory_names if hasattr(sdk, n)), None)
    if factory is None:
        result["verdict"] = (
            "PARTIAL — module imported but no engine factory found. "
            f"Looked for: {factory_names}. Available attrs: "
            f"{[n for n in dir(sdk) if not n.startswith('_')][:30]}"
        )
        print(json.dumps(result, indent=2))
        return 0

    try:
        engine = factory()
        # Destroy via common shapes; ignore failures (PoC doesn't care
        # about clean teardown, only that load+create works).
        for destroy_name in ("destroy", "Destroy", "release", "Release"):
            if hasattr(engine, destroy_name):
                with contextlib.suppress(Exception):
                    getattr(engine, destroy_name)()
                break
        result["sdk_engine_created"] = True
        result["verdict"] = (
            "PASS — frozen exe loaded SDK + created RTCEngine + destroyed it. "
            "Ship D1 7.x PyInstaller onedir path."
        )
        rc = 0
    except Exception as exc:  # noqa: BLE001
        result["verdict"] = (
            f"FAIL — SDK imported but engine creation crashed: "
            f"{exc.__class__.__name__}: {exc}"
        )
        rc = 1

    print(json.dumps(result, indent=2))
    return rc


if __name__ == "__main__":
    sys.exit(main())
