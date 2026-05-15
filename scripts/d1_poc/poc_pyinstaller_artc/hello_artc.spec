# PyInstaller spec for D1 PoC 2.4 — frozen onedir layout with ARTC SDK
# Windows binaries bundled.
#
# Run on a Windows machine:
#
#   set ISALES_RTC_SDK_WINDOWS_PATH=C:\path\to\aliyun-artc-windows-python
#   pyinstaller scripts/d1_poc/poc_pyinstaller_artc/hello_artc.spec
#   dist\hello_artc\hello_artc.exe
#
# Output:
#   - PASS / PARTIAL / FAIL JSON on stdout (see hello_artc.py)
#   - Exit code 0 (PASS or PARTIAL) or 1 (FAIL).
#
# Notes
# -----
# - `onedir` (not `onefile`) per design.md Open Question 3 + Decision 4
#   — startup speed matters more than single-exe convenience; D3 MSI
#   will hide the _internal directory from the user.
# - The SDK directory is copied verbatim under `vendor/aliyun-artc-
#   windows-python/` inside the frozen `_internal/` tree. The Python
#   wrapper does `import alirtc_sdk` (or similar); the bundled DLLs
#   sit next to the wrapper module so Windows DLL search finds them
#   when the wrapper is imported.
# - `hidden_imports = ['alirtc_sdk', ...]` covers wrapper names PyInstaller
#   would otherwise miss because they're dynamically loaded after we
#   insert the SDK dir into `sys.path` at runtime.

import os
from pathlib import Path

block_cipher = None
HERE = Path(SPECPATH).resolve()  # noqa: F821 — provided by PyInstaller

# Resolve ARTC SDK Windows path from env (so the same .spec works on
# any developer machine — no hard-coded paths).
sdk_env = os.environ.get("ISALES_RTC_SDK_WINDOWS_PATH", "")
sdk_path = Path(sdk_env) if sdk_env else None

# `datas` entries: (src_path_on_disk, dest_path_in_frozen_tree)
datas = []
if sdk_path is not None and sdk_path.exists():
    datas.append((str(sdk_path), "vendor/aliyun-artc-windows-python"))

a = Analysis(  # noqa: F821 — PyInstaller injects this
    [str(HERE / "hello_artc.py")],
    pathex=[str(HERE)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "aliyun_rtc_sdk",
        "alirtc_sdk",
        "AliRtcSdk",
        "aliyun_artc",
        "alirtc",
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="hello_artc",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
)
coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="hello_artc",
)
