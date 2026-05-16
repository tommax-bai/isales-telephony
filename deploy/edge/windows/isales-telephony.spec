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
# so we don't have to enumerate individual filenames; PyInstaller will
# silently no-op the glob if the dir is empty (CI builds without ARTC
# will produce a smoke-test binary that fails at RTC join — acceptable
# for `make build` smoke).
artc_binaries = []
import glob
import os

_HERE = os.path.dirname(os.path.abspath(SPEC))
_VENDOR_DIR = os.path.join(_HERE, "vendor", "aliyun-artc-windows")
if os.path.isdir(_VENDOR_DIR):
    for dll in glob.glob(os.path.join(_VENDOR_DIR, "*.dll")):
        # (source, destination) — pack into the runtime root so
        # ctypes.CDLL(name) finds it without explicit PATH magic.
        artc_binaries.append((dll, "."))
    for pyd in glob.glob(os.path.join(_VENDOR_DIR, "*.pyd")):
        artc_binaries.append((pyd, "."))

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
        # sounddevice / cffi: PortAudio wrapper.
        "sounddevice",
        "_cffi_backend",
        # ARTC SDK Python wrapper — name varies by vendor packaging; add
        # the canonical one. If the operator's bundle uses a different
        # module name they edit this list at build time.
        "aliyun_artc",
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
