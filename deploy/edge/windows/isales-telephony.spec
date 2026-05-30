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
# - RTC SDK DLLs + pybind11 binding: § 7.11 (engine-rtc-dingrtc-migration)
#   removed the ARTC SDK + aliyun_artc_pywrap path. § 7.9 (TODO) will wire
#   DingRTC: collect `~/codes/vendor/DingRTC_Windows_SDK_3_9_0/lib/x64/*.dll`
#   (12 DLLs incl. DingRTC.dll, ffmpeg, libSR, krt, hbal_se, libcrypto,
#   libssl, mediafoundation_capture) + dingrtc_pywrap.cp312-win_amd64.pyd
#   from build/dingrtc-binding/Release/ and add to `binaries` + add
#   "dingrtc_pywrap" hidden import. Current build produces RTC-less smoke
#   binary (modem + AT + cloud-edge gRPC work; RTC import fails).
# - Tray icon: `icons/tray.ico` ships as a `datas` entry; main_windows.py
#   reads it via sys._MEIPASS-aware path.

from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

import glob
import os

# RTC vendor DLLs + pybind11 binding .pyd: removed § 7.11 with the ARTC SDK.
# § 7.9 (TODO engine-rtc-dingrtc-migration) will collect from
# ~/codes/vendor/DingRTC_Windows_SDK_3_9_0/lib/x64/*.dll + build/dingrtc-
# binding/Release/dingrtc_pywrap.cp312-win_amd64.pyd; until then `rtc_binaries`
# stays empty so PyInstaller produces a smoke binary (modem/AT/gRPC work,
# RTC import will fail at runtime).
rtc_binaries = []

_HERE = os.path.dirname(os.path.abspath(SPEC))

# VC Runtime — DingRTC DLLs + future pybind11 binding both depend on
# msvcp140 / vcruntime140; bundle them so users don't need VC Redist.
# Kept active because installer needs them regardless of RTC bundling state.
_SYS32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
for vcrt_dll in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
    _src = os.path.join(_SYS32, vcrt_dll)
    if os.path.exists(_src):
        rtc_binaries.append((_src, "."))

_ICON_DIR = os.path.join(_HERE, "icons")
_TRAY_ICO = os.path.join(_ICON_DIR, "tray.ico") if os.path.exists(
    os.path.join(_ICON_DIR, "tray.ico")
) else None


a = Analysis(
    ["..\\..\\..\\isales_telephony\\main_windows.py"],
    pathex=[],
    binaries=rtc_binaries,
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
        # TODO § 7.9 (engine-rtc-dingrtc-migration): add "dingrtc_pywrap"
        # hidden import once § 7.9 wires the .pyd into `binaries` above.
        # Until then the entrypoint will fail at `import dingrtc_pywrap`
        # but only on the RTC code path (modem/AT/gRPC paths unaffected).
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
