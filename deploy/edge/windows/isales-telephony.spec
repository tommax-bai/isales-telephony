# PyInstaller spec for the iSales Windows edge client.
#
# Spec: windows-client-core / deployment-topology § Scenario
#   "PyInstaller 打包配置" + "Windows-specific 依赖".
#
# Build:
#     pyinstaller deploy/edge/windows/isales-telephony.spec --noconfirm
#
# Output:
#     dist/isales-telephony/
#         isales-telephony.exe         (frozen Windows console / GUI entrypoint)
#         _internal/                   (Python runtime, site-packages, DLLs)
#
# Notes:
# - mode: onedir (NOT onefile) — the spec § "PyInstaller 打包配置" pins this
#   because onefile bursts ~200-500 ms decompressing on every launch and
#   triggers more AV heuristics. After D3 wraps the dir in an MSI the
#   user never sees `_internal/`.
# - Console window: Hidden via `console=False`. The activation dialog +
#   tray are the only user-visible surface; stdout/stderr go to the
#   logging.FileHandler installed by main_windows.py.
# - ARTC SDK DLLs: drop the Aliyun ARTC-for-Windows native bundle under
#   `vendor/aliyun-artc-windows/` next to the spec before building. The
#   `binaries` glob below picks them up; the `hiddenimports` line ensures
#   the Python wrapper module name PyInstaller might miss is included.
# - Tray icon: `icons/tray.ico` ships as a `datas` entry; main_windows.py
#   reads it via sys._MEIPASS-aware path.

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# ARTC SDK Windows native — operator-supplied. Glob over the vendor dir
# (recursively, since v7.6.0 puts DLLs under x64/Release/) and also pick
# up the project-local pybind11 binding .pyd from pybind/. PyInstaller
# silently no-ops missing globs, so CI builds without ARTC still produce
# a smoke-test binary that fails at RTC import — acceptable.
artc_binaries = []
import glob
import os

_HERE = os.path.dirname(os.path.abspath(SPEC))
_VENDOR_DIR = os.path.join(_HERE, "vendor", "aliyun-artc-windows")
if os.path.isdir(_VENDOR_DIR):
    for dll in glob.glob(os.path.join(_VENDOR_DIR, "**", "*.dll"), recursive=True):
        # (source, destination) — pack into the runtime root so
        # ctypes.CDLL(name) finds it without explicit PATH magic.
        artc_binaries.append((dll, "."))

# Project-local pybind11 binding — built by build.ps1's CMake step.
# See openspec/changes/windows-artc-pybind11/.
_PYBIND_DIR = os.path.join(_HERE, "pybind", "aliyun_artc_pywrap")
if os.path.isdir(_PYBIND_DIR):
    for pyd in glob.glob(os.path.join(_PYBIND_DIR, "*.pyd")):
        artc_binaries.append((pyd, "."))

# VC Runtime — Aliyun's DLLs + the pybind11 binding both depend on
# msvcp140 / vcruntime140; bundle them so users don't need VC Redist.
# See openspec/changes/windows-artc-pybind11/design.md Decision 8.
_SYS32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
for vcrt_dll in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
    _src = os.path.join(_SYS32, vcrt_dll)
    if os.path.exists(_src):
        artc_binaries.append((_src, "."))

_ICON_DIR = os.path.join(_HERE, "icons")
_TRAY_ICO = os.path.join(_ICON_DIR, "tray.ico") if os.path.exists(
    os.path.join(_ICON_DIR, "tray.ico")
) else None


a = Analysis(
    ["..\\..\\..\\isales_telephony\\main_windows.py"],
    pathex=[],
    binaries=artc_binaries,
    datas=[
        (_ICON_DIR, "icons"),
        # env.example.txt ships at the binary root for first-time setup.
        (os.path.join(_HERE, "env.example.txt"), "."),
        # VC Runtime redistribution attribution (Microsoft EULA terms).
        (os.path.join(_HERE, "licenses"), "licenses"),
    ],
    hiddenimports=[
        # PySide6 / qasync occasionally miss the submodule scan.
        "PySide6.QtCore",
        "PySide6.QtWidgets",
        "PySide6.QtGui",
        "qasync",
        # pystray + PIL: pystray imports its Windows backend lazily.
        "pystray._win32",
        "PIL.Image",
        "PIL.ImageDraw",
        # SerialPcm-over-COM replaced WASAPI as the v1.0 audio backend
        # (windows-client-core design.md Decision 3 amend 2026-05-17);
        # sounddevice / _cffi_backend hidden imports removed accordingly.
        # pyserial is already a main dependency and visible to PyInstaller.
        # Project-local pybind11 binding for the ARTC Windows SDK. The
        # .pyd ships via `binaries` glob; this hidden import ensures
        # PyInstaller's depscan treats it as a module reference.
        # See openspec/changes/windows-artc-pybind11/.
        "aliyun_artc_pywrap",
        # grpcio runtime helpers PyInstaller sometimes misses.
        *collect_submodules("grpc"),
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Cut Linux-only deps — pyudev / pyalsaaudio have no Windows wheel
        # and PyInstaller's depscan would warn on them anyway.
        "pyudev",
        "alsaaudio",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="isales-telephony",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX flags AV; not worth the size win.
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_TRAY_ICO,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="isales-telephony",
)
