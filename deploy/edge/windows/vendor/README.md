# Vendor binaries for the Windows edge client

This directory holds proprietary third-party SDKs that the iSales
Windows edge client links against. Contents are **gitignored** — the
operator drops the unpacked SDK here before running
`deploy/edge/windows/build.ps1`.

## Aliyun ARTC SDK for Windows

**Expected layout** (after `Expand-Archive` of the official zip):

```
aliyun-artc-windows/
├── include/                     # C++ headers (rtc/, player/, pusher/)
│   ├── alivc_live_define.h
│   ├── alivc_live_listener.h
│   ├── alivc_live_utils.h
│   ├── rtc/
│   │   ├── engine_define.h
│   │   ├── engine_device_manager.h
│   │   ├── engine_interface.h         # 299 KB — main AliEngine + EventListener
│   │   ├── engine_media_engine.h      # IAudioFrameObserver, AliEngineAudioRawData
│   │   └── engine_utils.h
│   ├── player/...
│   └── pusher/...
└── x64/Release/                 # 64-bit binaries (~35 MB total)
    ├── AliRTCSdk.dll            # 22 MB — RTC main DLL
    ├── AliRTCSdk.lib            # 0.2 MB — link library
    ├── alivcffmpeg.dll          # 3.7 MB
    ├── alivcx265.dll            # 5.9 MB — H.265 (not used by audio path, runtime needs it)
    ├── PluginAAC.dll            # 1.2 MB
    └── x264.dll                 # 2.4 MB
```

**Source**: official zip from Aliyun CDN
`https://alivc-demo-cms.alicdn.com/versionProduct/sdk/rtc/windows/AliVCSDK_ARTC-7.6.0.zip`
(v7.6.0, dated 2025-09-02), linked from
[阿里云 ARTC SDK 下载页](https://help.aliyun.com/zh/live/artc-download-the-sdk).

**Drop `.pdb` debug symbols** before committing/packaging — `*.pdb`
files inflate the SDK to ~170 MB and aren't needed at runtime.

## Important caveat: no official Python wrapper for Windows

Unlike the **Linux** ARTC SDK (which ships an official Python wrapper —
see `isales-engine/transport/_rtc_sdk.py`), the **Windows** SDK is
**C++ only** (headers + `.lib` + `.dll`). The iSales Windows edge
client therefore needs a project-local pybind11 binding to call into
the RTC engine.

That binding lives under
`deploy/edge/windows/pybind/aliyun_artc_pywrap/` and produces
`aliyun_artc_pywrap.pyd` — packaged into the PyInstaller frozen exe
alongside the ARTC DLLs.

**Implementation status (2026-05-17)**: implemented. The binding sources
`bindings.cpp` / `audio_observer.{h,cpp}` / `engine_listener.{h,cpp}` /
`ring_buffer.{h,cpp}` live in `pybind/aliyun_artc_pywrap/src/`. CMake
build is wired into `deploy/edge/windows/build.ps1` before PyInstaller
runs. The OpenSpec change `openspec/changes/windows-artc-pybind11/`
captures the design (pybind11 + CMake / GIL strategy / lifecycle).
See also the `reference-artc-sdk` memory note for the Linux-vs-Windows
SDK shape discrepancy that originally drove this work.

## What `build.ps1` expects to find here

The PyInstaller spec (`deploy/edge/windows/isales-telephony.spec`)
globs `**/*.dll` recursively from `vendor/aliyun-artc-windows/` (the
v7.6.0 zip puts them under `x64/Release/`) and globs `*.pyd` from
`pybind/aliyun_artc_pywrap/` (the CMake build output, see build.ps1
step 4). If `vendor/` is empty the build still succeeds, but
`build.ps1` skips the CMake step and the resulting frozen exe will
fail at `import aliyun_artc_pywrap` — useful for catching wire-up
bugs without needing the SDK in CI.
