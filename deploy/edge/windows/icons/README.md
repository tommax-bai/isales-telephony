# Tray icons

`tray.ico` is the Windows tray icon embedded in the PyInstaller frozen
binary. The current file is a 16x16 single-image placeholder generated
during D1 implementation (windows-client-core task 7.6) — solid brand
blue (#1A73E8).

For the production v1.0 release, replace `tray.ico` with the brand asset:

- Multi-resolution ICO (16x16, 24x24, 32x32, 48x48, 64x64, 128x128, 256x256)
- 32-bit BGRA with proper alpha mask
- Generated from the iSales v1.0 brand SVG (designer hand-off)

D3 (`windows-installer-and-ota`) will swap this placeholder for the
final asset; the path / filename SHALL stay `icons/tray.ico` so the
PyInstaller spec doesn't need a rebuild test.

The tray icon `.ico` is consumed at:

- `isales-telephony.spec` → `EXE(icon=...)` for the exe metadata
- `_internal/icons/tray.ico` at runtime (via the `datas` entry) — not
  currently read by `main_windows.py` (it draws the active green/red
  icon via PIL in `ui/tray.py::_build_icon_image`); reserved for future
  notification fallback.
