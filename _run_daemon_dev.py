"""Dev launcher: DingRTC DLL wiring + main_windows.run (loads telephony.env).

main_windows is the production Windows entry (reads %APPDATA%/isales/env/
telephony.env, gRPC heartbeat resets device status) but does NOT wire the
DingRTC vendor DLL dir (relies on frozen-exe co-location). This launcher
adds the DLL dir like windows_dingrtc_session_smoke.py, then runs it.
Run under .venv-3.12 (cp312 ABI).
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_vendor = os.path.expandvars(
    r"%USERPROFILE%\codes\vendor\DingRTC_Windows_SDK_3_9_0\lib\x64"
)
if os.path.isdir(_vendor):
    os.add_dll_directory(_vendor)
else:
    sys.stderr.write(f"WARN: vendor DLL dir not found: {_vendor}\n")
_pyd_dir = os.path.join(_HERE, "build", "dingrtc-binding", "Release")
if os.path.isdir(_pyd_dir):
    sys.path.insert(0, _pyd_dir)
else:
    sys.stderr.write(f"WARN: pyd dir not found: {_pyd_dir}\n")

from isales_telephony.main_windows import run  # noqa: E402

if __name__ == "__main__":
    run()
