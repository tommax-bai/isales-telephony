# aliyun_artc_pywrap

Project-local pybind11 binding for the Aliyun ARTC SDK for Windows.

**Why this exists**: Aliyun publishes a Python wrapper for the Linux ARTC
SDK but **not** for Windows — the Windows SDK is pure C++ (headers +
`.lib` + `.dll`). iSales' Windows edge client needs an asyncio-friendly
Python entry point, so this directory contains a minimal pybind11
binding that exposes exactly the audio path the edge needs: create /
destroy engine, join / leave channel, push external audio, receive
remote PCM via an observer.

See `openspec/changes/windows-artc-pybind11/` for the full design.

## Layout

```
.
├── CMakeLists.txt              # build configuration
├── src/
│   ├── bindings.cpp            # PYBIND11_MODULE entry; EngineHandle wrapper
│   ├── audio_observer.{h,cpp}  # IAudioFrameObserver → ring buffer
│   ├── engine_listener.{h,cpp} # AliEngineEventListener → Python callables
│   └── ring_buffer.{h,cpp}     # MPSC PCM frame queue
├── deps/
│   └── pybind11/               # git submodule, header-only
└── aliyun_artc_pywrap.pyd      # build output (gitignored)
```

The binding depends on the vendor SDK at
`../../vendor/aliyun-artc-windows/` (gitignored, see that directory's
`README.md` for the download URL).

## Build

`build.ps1` (one directory up) drives this automatically. To build
manually:

```powershell
# From this directory:
cd deploy\edge\windows\pybind\aliyun_artc_pywrap

# First-time: ensure the pybind11 submodule is checked out.
git submodule update --init --recursive

# Configure + build (Release).
cmake -S . -B ..\..\..\..\build\pybind `
    "-DPython3_EXECUTABLE=$((Get-Command python).Source)" `
    "-DCMAKE_BUILD_TYPE=Release"
cmake --build ..\..\..\..\build\pybind --config Release --target aliyun_artc_pywrap

# Copy the .pyd next to the source dir so PyInstaller's `binaries`
# glob picks it up. build.ps1 does this automatically.
Copy-Item -Path "..\..\..\..\build\pybind\Release\aliyun_artc_pywrap.pyd" `
          -Destination ".\aliyun_artc_pywrap.pyd" -Force

# Smoke import.
python -c "import sys; sys.path.insert(0, '.'); import aliyun_artc_pywrap; print(aliyun_artc_pywrap.__version__)"
```

Toolchain requirements:

- Python 3.12.x (must match the PyInstaller bundled interpreter)
- CMake 3.20+
- Visual Studio Build Tools 2022 (C++ workload + Windows SDK 10.0.22621)
- pybind11 via submodule (do NOT install via pip / vcpkg — version drift)

## Testing

Python-side unit tests (mock binding, runs anywhere):

```bash
pytest tests/windows/test_windows_rtc_session.py -v
```

C++ binding smoke testing happens through `WindowsRtcSession` integration
tests; the binding itself has no separate GoogleTest harness (Decision
in design.md: keep CI minimal, exercise the binding through Python).

Real RTC join testing (requires vendor SDK + a real Aliyun RTC AppId
configured) is gated by `@pytest.mark.requires_artc_sdk` and runs only
on Windows CI runners with credentials. See `tests/windows/` for the
hardware acceptance pattern.

## Upgrading the SDK

When Aliyun publishes a new ARTC SDK Windows version:

1. Drop the new zip into `../../vendor/aliyun-artc-windows/`, update
   the version line in `vendor/README.md`.
2. Re-run `build.ps1`. Fix any compile errors here (the SDK's C++ API
   is reasonably stable; expect maybe one or two enum renames).
3. Re-run `tests/windows/test_windows_rtc_session.py` (mock path) +
   the real-SDK acceptance smoke (`pytest -m requires_artc_sdk`).
4. Re-run windows-client-core's §2.4 and §9.3 acceptance steps.

## Design notes

The binding deliberately exposes a **low-level** API surface that
mirrors the SDK's own C++ shape. The asyncio-friendly façade lives in
`isales_telephony.audio_bridge.windows_rtc_session.WindowsRtcSession`,
which is what the `audio-bridge` and tests use. Keep this binding
layer thin — anything you can do in Python should live in Python, not
here.

Audio frame callbacks run on the SDK's audio I/O thread (~50 Hz);
the binding's `AudioObserver` pushes them to a thread-safe ring
buffer **without acquiring the GIL**. The Python drainer task pulls
batched frames per ~5 ms tick. Event callbacks (join / leave /
connection lost / error) run at <10 Hz; they take the GIL via
`pybind11::gil_scoped_acquire` and dispatch to Python callables.
Python exceptions raised inside event callbacks are caught and logged
via `PyErr_WriteUnraisable` — they never propagate back into SDK
C++ frames.
