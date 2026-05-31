"""Dev launcher for the headless edge daemon with DingRTC binding wired.

The production frozen exe (build.ps1) co-locates the vendor DLLs next to the
.pyd; for a dev run we instead arrange the DLL search path + .pyd sys.path
BEFORE importing anything that pulls in ``dingrtc_pywrap``. Must run under the
Python that matches the .pyd ABI (cp312 → .venv-3.12).

Usage (env must already be exported):
    .venv-3.12/Scripts/python.exe run_edge_daemon.py
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# 1. Vendor DLLs (DingRTC.dll + ffmpeg/libSR/krt/hbal_se/openssl/...) onto the
#    DLL search path so the .pyd's dependencies resolve.
_vendor = os.path.expandvars(
    r"%USERPROFILE%\codes\vendor\DingRTC_Windows_SDK_3_9_0\lib\x64"
)
if os.path.isdir(_vendor):
    os.add_dll_directory(_vendor)
else:
    sys.stderr.write(f"WARN: vendor DLL dir not found: {_vendor}\n")

# 2. Built .pyd directory onto sys.path so ``import dingrtc_pywrap`` resolves.
_pyd_dir = os.path.join(_HERE, "build", "dingrtc-binding", "Release")
if os.path.isdir(_pyd_dir):
    sys.path.insert(0, _pyd_dir)
else:
    sys.stderr.write(f"WARN: pyd dir not found: {_pyd_dir}\n")

from isales_telephony.edge.main import run  # noqa: E402

if __name__ == "__main__":
    run()
