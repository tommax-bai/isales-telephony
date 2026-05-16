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

That binding lives (or will live, depending on which OpenSpec change
is active) under
`deploy/edge/windows/pybind/aliyun_artc_pywrap/` and produces
`aliyun_artc_pywrap.pyd` — packaged into the PyInstaller frozen exe
alongside the ARTC DLLs.

See `openspec/changes/windows-artc-pybind11/` (proposal, pending) and
the `reference-artc-sdk` memory note for the rationale and the
estimated 1-week effort.

## What `build.ps1` expects to find here

The PyInstaller spec (`deploy/edge/windows/isales-telephony.spec`)
globs `*.dll` and `*.pyd` from `vendor/aliyun-artc-windows/`. If this
directory is empty the build succeeds but the resulting exe will fail
at RTC join (a smoke-test outcome — useful for catching wire-up bugs
without the SDK in CI).
