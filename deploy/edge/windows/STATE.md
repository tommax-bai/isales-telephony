# Windows edge dev rig — current state snapshot

**Last updated**: 2026-05-30 — DingRTC Windows SDK 3.9.0 vendored at
`~/codes/vendor/DingRTC_Windows_SDK_3_9_0/` (sha256 matches mac end-of-tasks.md §1.3);
ARTC SDK + `aliyun_artc_pywrap.pyd` retained but **superseded** — see § "DingRTC
SDK for Windows" below; `aliyun-artc-windows/` + `aliyun_artc_pywrap/` to be
removed after Windows `dingrtc_pywrap` binding builds green + join smoke passes
(`engine-rtc-dingrtc-migration` §7). Toolchain (Python 3.12 + CMake + VS BuildTools)
unchanged; SIM7600G-H modem on COM12 (AT) / COM11 (audio); cloud-edge gRPC smoke
to ECS green.

This file is the Windows-dev-rig sibling of
`isales/deploy/cloud/STATE.md`. It records the state of the **build
environment + edge runtime + connected hardware** on the developer's
Windows machine. Reading this before reporting any
"Windows-side X is missing" prevents the regression class documented
in `[[feedback-ground-truth-before-pending]]` memory.

> **Fresh Claude Code session resuming work — READ THIS FIRST**. The
> OpenSpec tasks.md checkboxes for `windows-artc-pybind11` and any
> active `windows-*` change lag the actual install + build state. This
> file is canonical for the dev box; CLAUDE.md (root) makes STATE.md
> files the arbiter when RUNBOOK / OpenSpec disagree. See § "Bootstrap
> a new dev session" at the bottom for the verification recipe.

## Host

| Field | Value |
|---|---|
| OS | Windows 11 25H1 (`Microsoft Windows [版本 10.0.26100.x]`) |
| Arch | x86_64 |
| User | `tianx` |
| Repo root | `C:\Users\tianx\codes\` (7 sibling repos flat-laid: isales meta + isales-common + isales-api + isales-engine + isales-scheduler + isales-worker + isales-telephony + isales-web) |
| SSH key for ECS | `C:\Users\tianx\codes\isales-4.pem` (2026-05-24 rotation; 验 `ssh-keygen -lf` 该路径)。历史: `isales.pem` (2026-05-17 install-time, 已失效) → `isales-3.pem` (2026-05-19 mac rotation, 已失效) → `isales-4.pem` (2026-05-24, current) |

## Installed toolchain (added 2026-05-17 by `winget`-or-direct-installer flow)

| Tool | Version | Path | Notes |
|---|---|---|---|
| Python 3.14.4 | 3.14.4 | `C:\Users\tianx\AppData\Local\Python\bin\python.exe` | original dev Python; **incompatible with pybind binding ABI** |
| Python 3.12.10 | 3.12.10 | `C:\Users\tianx\AppData\Local\Programs\Python\Python312\python.exe` | **required for pybind .pyd build** (matches PyInstaller bundle + ABI lock per `windows-artc-pybind11/design.md` Decision 7). Invoke via `py -3.12`. |
| CMake | 4.3.2 | `C:\Program Files\CMake\bin\cmake.exe` | Added to user PATH 2026-05-17 (`[Environment]::SetEnvironmentVariable("Path", ..., "User")`); new PowerShell sessions see `cmake` on PATH |
| MSBuild / MSVC compiler | MSVC 19.44.35227.0 (toolset 14.44.35207) | `C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC\14.44.35207\bin\Hostx64\x64\cl.exe` | Visual Studio 2022 Build Tools `Microsoft.VisualStudio.Workload.VCTools` + Windows 11 SDK (`includeRecommended`) |
| Git | (bundled with VS BuildTools / system) | `C:\Program Files\Git\` likely | Used by repo workflow |

Initial `winget install` attempt deadlocked (4 parallel `winget` processes
hung 30 min with 0 TCP connections — likely COM serialization + slow
GitHub release fetch). Final working path:
1. `winget install Python.Python.3.12` (succeeded standalone)
2. `winget install Kitware.CMake` (succeeded) + manual user PATH update
3. `winget install Microsoft.VisualStudio.2022.BuildTools` only placed the
   shell; full VCTools workload installed by re-running the official
   `vs_BuildTools.exe modify --add Microsoft.VisualStudio.Workload.VCTools`
   bootstrapper directly.

The `scripts/d1_poc/WINDOWS_SETUP.md` doc inside the isales-telephony
repo is the canonical end-user install recipe for these tools.

## Python virtual envs

| venv path | Python | Purpose |
|---|---|---|
| `C:\Users\tianx\codes\isales-telephony\.venv\` | 3.14.4 | Original dev + test venv. PR #6 → #13 tests pass here. `pip install -e ".[dev]"` already done; `grpcio` added 2026-05-17 for `scripts/cloud_edge_smoke.py`. |
| `C:\Users\tianx\codes\isales-telephony\.venv-3.12\` | 3.12.10 | **Pybind build venv only**. Used as `-DPython3_EXECUTABLE=...` for CMake configure; `aliyun_artc_pywrap.cp312-win_amd64.pyd` ABI tied to its Python. Not used for tests / dev. |

`.gitignore` covers `.venv*/` (widened 2026-05-17 commit `b9cd5de`) so
both venvs stay out of git.

## DingRTC SDK for Windows (vendored, gitignored)

Location: `C:\Users\tianx\codes\vendor\DingRTC_Windows_SDK_3_9_0\` (cross-repo
shared vendor dir, **not** under `isales-telephony/`; matches three-end
convention in `engine-rtc-dingrtc-migration/design.md` D9 — Linux uses
`/opt/isales/vendor/DingRTC_Linux_SDK_3_9_0/`, macOS uses
`~/codes/vendor/DingRTC_macOS_SDK_3_9_0/`).

Vendor zip: `dingrtc.oss-cn-zhangjiakou.aliyuncs.com/sdk/windows/3.9.0/DingRTC_Windows_SDK_3_9_0.zip`
(48.44 MB, 50797083 bytes).

| Field | Value |
|---|---|
| Version | 3.9.0 (2025-04-15 release; three-end interop locked at same minor+patch per design.md D2) |
| Downloaded | 2026-05-30 via `Invoke-WebRequest` |
| SHA256 (zip) | `F59482589A211E3FC4368C4750DFA50603F03C0ACDF3D157728A41AC1CA19974` — **cross-platform match** with mac end's record in `engine-rtc-dingrtc-migration/tasks.md` §1.3 (`f59482589a211e3fc4368c4750dfa50603f03c0acdf3d157728a41ac1ca19974`); proves OSS-side zip identity across dev rigs |
| Official doc | https://help.aliyun.com/zh/document_detail/2667835.html |

Tree:

```
vendor/DingRTC_Windows_SDK_3_9_0/
├── api/             # 11 C++ headers (-I 用)
│   ├── engine_interface.h            (93 KB, 主入口 API)
│   ├── engine_types.h                (54 KB, 类型/枚举/结构体)
│   ├── engine_audio_mixing_manager.h ( 8 KB)
│   ├── engine_device_manager.h       (20 KB)
│   ├── engine_rtm.h                  (16 KB)
│   ├── engine_subtitle_manager.h     ( 4 KB)
│   ├── engine_utils.h                ( 2 KB)
│   ├── engine_wb_interface.h         (28 KB, whiteboard - 不用)
│   ├── engine_wb_types.h             (14 KB, whiteboard - 不用)
│   ├── engine_conf.h                 ( 1 KB)
│   └── README
├── lib/
│   ├── x64/                          # ← Windows binding 用这个（Python 3.12 x64 ABI 对齐）
│   │   ├── DingRTC.lib   ( 90 KB)    # link-time import lib
│   │   ├── DingRTC.dll   (27 MB)     # runtime, main SDK
│   │   ├── ffmpeg.dll    (38 MB)     # runtime dep
│   │   ├── libSR.dll     (9.4 MB)    # 语音识别 runtime dep
│   │   ├── krt.dll       (158 KB)    # vendor runtime support
│   │   ├── hbal_se.dll   (2.0 MB)    # SE (signal enhancement) runtime dep
│   │   ├── libcrypto-1_1-x64.dll + libssl-1_1-x64.dll  # OpenSSL 1.1 runtime
│   │   ├── mediafoundation_capture.dll                 # Win MF capture
│   │   ├── DingBeauty.dll + MoziWhiteboard.dll + pdfium.dll  # 视频/白板/PDF 旁路功能，
│   │   │                                                       不用但 vendor 默认带
│   │   └── kashost.exe                                 # SDK 后台 helper 进程（按需）
│   └── x86/             # 备用，不用（我们走 64-bit Python）
└── model/
    ├── dingseg.mnn        (582 KB)   # MNN model — 语音分段
    └── hbal_se_v1.0.15.nn (1.6 MB)   # 神经网络 — 降噪
```

**`.gitignore` coverage**: `isales-telephony/.gitignore` already includes
`vendor/DingRTC_*_SDK_*/` glob (`engine-rtc-dingrtc-migration/tasks.md` §1.6).
The vendor dir is **outside** `isales-telephony/` so this is belt-and-suspenders
— `~/codes/vendor/` is not inside any of the 7 sibling repos and is not git-managed.

**Runtime DLL co-location (verify when building binding)**: the 10+ DLLs in
`lib/x64/` are inter-dependent. When the pybind `.pyd` loads `DingRTC.dll`,
Windows loader will look for `ffmpeg.dll` / `krt.dll` / `libSR.dll` / `hbal_se.dll`
/ both `libcrypto-1_1-x64.dll` + `libssl-1_1-x64.dll` / `mediafoundation_capture.dll`
either next to `.pyd`, on `PATH`, or via `SetDllDirectory` / `os.add_dll_directory`.
Cloud Linux model uses `-rpath` + same-dir; Windows equivalent is to copy
all `lib/x64/*.dll` next to the built `.pyd` (this is what
`build.ps1` does for the existing ARTC binding — same pattern applies).
**Open question**: whether `kashost.exe` is required at runtime or is a CLI helper
— check vendor doc §"集成准备" / quickstart before §7.4 build.

**Used by**: `deploy/edge/windows/pybind/dingrtc_pywrap/` (new in
`engine-rtc-dingrtc-migration` §7; layout mirrors cloud's
`isales-engine/deploy/cloud/pybind/dingrtc_pywrap/` rather than
spec §7.1 字面的 `isales_telephony/audio_bridge/_dingrtc_pywrap_src/` —
对齐既有 Windows ARTC binding 的 `deploy/edge/windows/pybind/` 布局，与 cloud §2.1
layout deviation 同理由）.

---

## Aliyun ARTC SDK for Windows [LEGACY — superseded by DingRTC 3.9.0 on 2026-05-30]

> **Status**: vendor binary + `aliyun_artc_pywrap.pyd` 还在 `vendor/aliyun-artc-windows/`
> 与 `pybind/aliyun_artc_pywrap/`，**未删** —— 留作 DingRTC binding build 失败时的 fallback
> 验证手段。`engine-rtc-dingrtc-migration` §7 完整 green（含 §7.11 旧 binding 清理）
> 后整段移除（包括 vendor + binding 源 + 本节）。
>
> **根因**: ARTC SDK 是 ApsaraVideo Live 产品线，与 ECS AppId `o6dpsan9`
> (DingRTC PaaS) 不互通，token 跨产品 fail with `ERR_JOIN_BAD_TOKEN 0x02010205`.
> 详见 `engine-rtc-dingrtc-migration/proposal.md`.

Location: `deploy/edge/windows/vendor/aliyun-artc-windows/`.

```
vendor/aliyun-artc-windows/
├── include/          # 头文件（编译时 -I）
│   └── rtc/
│       ├── engine_device_manager.h    # 含 C7626 触发点 line 51 (typedef struct with member init)
│       └── ... (其余 ARTC C++ headers)
└── x64/
    └── Release/
        ├── AliRTCSdk.lib        # link-time import lib for .pyd
        ├── AliRTCSdk.dll        (21.7 MB)  ← runtime dependency
        ├── alivcffmpeg.dll      (3.7 MB)
        ├── alivcx265.dll        (5.8 MB)
        ├── PluginAAC.dll        (1.2 MB)
        └── x264.dll             (2.4 MB)
```

Vendor source: `https://alivc-demo-cms.alicdn.com/versionProduct/sdk/rtc/windows/AliVCSDK_ARTC-7.6.0.zip`
(per `vendor/README.md`). 7.6.0 is the current pinned version; Aliyun's
desktop SDK release cadence is slow (Android / iOS at 7.11.0+).

**MSVC 19.44 + `/permissive-` regression**: vendor header
`engine_device_manager.h:51` uses `typedef struct { int width = 0; ... }
AliEngineVideoResolution;` (anonymous typedef with in-class member
initializers), valid under MS extensions but rejected by C7626 under
strict conformance. `CMakeLists.txt` for the pybind binding dropped
`/permissive-` 2026-05-17 (commit `b9cd5de`); our 4 binding `.cpp` files
are C++17-conformant by design so losing `/permissive-` removes a
defensive check without practical risk. Do **NOT** re-enable
`/permissive-` without first patching the vendor header or convincing
Aliyun to ship a newer toolchain build.

## pybind11 binding build (`dingrtc_pywrap`)  ← active

Source: `isales-telephony/deploy/edge/windows/pybind/dingrtc_pywrap/`
(8 files, 1049 lines; fork of `isales-engine/deploy/cloud/pybind/dingrtc_pywrap/`
from `origin/dingrtc-migration-cloud`; CMake Linux→Windows retarget per §7.2).

| File | Purpose |
|---|---|
| `CMakeLists.txt` | `find_package(Python3 3.12 EXACT REQUIRED)`; pybind11 via `python -m pybind11 --cmakedir` (no submodule); imports `DingRTC.lib` + `DingRTC.dll` from `$USERPROFILE/codes/vendor/DingRTC_Windows_SDK_3_9_0/lib/x64/`; env override `ISALES_DINGRTC_WINDOWS_SDK_PATH` |
| `src/bindings.cpp` | Exposes `EngineHandle` SDK wrapper + 8 supporting types (unchanged from cloud) |
| `src/audio_observer.cpp` + `.h` | `AudioObserver : public IRtcEngineAudioFrameObserver` — pushes inbound PCM → `FrameRingBuffer` |
| `src/engine_listener.cpp` + `.h` | `EngineListener : public IRtcEngineEventListener` — 3 setter-injected Python callbacks (join / leave / error) |
| `src/ring_buffer.cpp` + `.h` | `FrameRingBuffer` — mutex-protected MPSC, drop-oldest on overflow |

**Build environment**:
- `.venv-3.12` 新加 dep: `pybind11==3.0.4` (`pip install pybind11` 2026-05-30)
- Output `.pyd`: `build/dingrtc-binding/Release/dingrtc_pywrap.cp312-win_amd64.pyd`

Manual build invocation:

```powershell
$tel = "C:\Users\tianx\codes\isales-telephony"
$env:Path = "C:\Program Files\CMake\bin;" + $env:Path
$py = "$tel\.venv-3.12\Scripts\python.exe"
$pybindDir = "$tel\deploy\edge\windows\pybind\dingrtc_pywrap"
$buildDir = "$tel\build\dingrtc-binding"

cmake -S $pybindDir -B $buildDir `
    "-DPython3_EXECUTABLE=$py" "-DCMAKE_BUILD_TYPE=Release"
cmake --build $buildDir --config Release --target dingrtc_pywrap
```

**Build + import + JOIN smoke 实测 GREEN (2026-05-30)**:

- CMake configure 5.4s; cmake --build 零 warnings (DingRTC vendor 头 MSVC-clean,
  与 ARTC vendor 头 C7626 anonymous typedef 不同; `/permissive-` 未来可补)
- 9 exported symbols: `AudioObserver` / `DingRtcError` / `EngineHandle` /
  `EngineListener` / `FrameRingBuffer` / `PcmFrame` / `POSITION_PLAYBACK` /
  `POSITION_REMOTE_USER` / `set_log_dir_path`
- Vendor demo path JOIN PASS via `scripts/windows_dingrtc_smoke.py`:
  - `OnJoinChannelResult: rc=0 (0x00000000)` channel=`win-smoke-1780112455`
    elapsed=500ms
  - SDK 内部日志: `[API] JoinChannel (appid=a4zfr1hn, channel=..., userid=win-smoke, gslb=https://gslb.dingrtc.com, ...)`
  - 5s 持续 + clean leave + destroy 无错
  - Token 来自 vendor `onertc-demo-app-server.dingtalk.com/login` (server-issued,
    无客户端 AppKey 参与; design.md §1.5 ENV path 已被 §4.2 ground-truth retire,
    与 mac smoke `fetch_demo_token` 同模式)

Smoke 命令:

```powershell
.\.venv-3.12\Scripts\python.exe scripts\windows_dingrtc_smoke.py
# 默认 demo path; 5s duration; PASS exit 0
# --real 走 ISALES_RTC_APP_ID + ISALES_RTC_APP_KEY 自签 token (生产 AppId)
```

**Runtime DLL co-location (next-to-pyd or add_dll_directory)**: smoke script
通过 `os.add_dll_directory(vendor/lib/x64)` 加 DLL 搜索路径; build.ps1 frozen-exe
路径需要把 `lib/x64/*.dll` (DingRTC + ffmpeg + libSR + krt + hbal_se + crypto
+ ssl + mediafoundation_capture + DingBeauty/MoziWhiteboard/pdfium) 复制到 .pyd
同目录. `kashost.exe` 实测 demo join 5s 周期未需要 spawn (vendor 文档待查
确认是否仅按需 launch), 当前不打 bundle.

**§7.6 + §7.7 实测 GREEN (2026-05-30)** via
`scripts/windows_dingrtc_session_smoke.py` (high-level
`WindowsDingRtcSession` wrapper + push_audio + drain):

- WindowsDingRtcSession constructed (app_id='a4zfr1hn',
  gslb='https://gslb.dingrtc.com')
- `await session.join(...)` wall_elapsed=453ms, is_joined=True ✓
- `push_audio(silence, ts)` 2s × 16kHz mono int16 = 64000 bytes
  全推完, 10ms 切片 + 8ms pace 工作正常
- `audio_frames()` async generator drain 4s 拿到 **204 inbound
  frames / 391680 bytes** (first_pcm_len=1920 = 960 samples = 60ms;
  ≈ 50 fps SDK 帧率)
- SDK 内部 log: `Uploader::OnStart` ×2 + `Uploader::OnProcess` ×N +
  `Uploader::OnEnd` (上行管道完整)
- `await session.leave()` clean + exit 0

Still to do (§7.8+):
- §7.8 dual-peer e2e (Windows + mac/ECS 同 channel 验真实 remote 音频)
- §7.9 build.ps1 集成 + DLL co-location
- §7.10 PyInstaller frozen-exe smoke
- §7.11 ARTC binding + vendor 清理 + `__init__.py` 路由切到
  WindowsDingRtcSession 默认 (currently 路由仍指 WindowsRtcSession,
  breaking-change 留 §7.8 e2e green 后一次性切)

## pybind11 binding build (`aliyun_artc_pywrap`) [LEGACY — superseded by `dingrtc_pywrap`]

> **Status**: 旧 binding 已 build (`.pyd` 在 `pybind/aliyun_artc_pywrap/`)，import smoke
> 实测 12 个符号（多出 6 个后期 enum，比 STATE.md 原写"6 expected"漂移）。**留着不删**，
> 同 vendor [LEGACY] 节理由 — 新 DingRTC binding 验证完整 green 前不删。
> `engine-rtc-dingrtc-migration` §7.11 时一并清理（含本节 + `pybind/aliyun_artc_pywrap/`
> + build artifacts）。新的 DingRTC binding 章节将在 §7 build 出 `.pyd` 时新增。

Source: `isales-telephony/deploy/edge/windows/pybind/aliyun_artc_pywrap/`

| File | Purpose |
|---|---|
| `CMakeLists.txt` | CMake project; `find_package(Python3 3.12 EXACT REQUIRED)`; `pybind11_add_module(aliyun_artc_pywrap MODULE src/*.cpp)` |
| `src/bindings.cpp` | Exposes `EngineHandle` class (SDK wrapper) + 5 pybind11 types |
| `src/audio_observer.cpp` + `.h` | `AudioObserver : public IAudioFrameObserver` — pushes inbound PCM frames to `FrameRingBuffer` |
| `src/engine_listener.cpp` + `.h` | `EngineListener : public AliEngineEventListener` — 4 setter-injected Python callbacks (join / leave / network / connection-state) |
| `src/ring_buffer.cpp` + `.h` | `FrameRingBuffer` — mutex-protected MPSC, drop-oldest on overflow |
| `deps/pybind11/` | git submodule, pybind11 v3.0.4 (stable; compatible with our v2.11+ requirement) |

Build invocation (manual, bypassing `build.ps1`'s frozen-exe wrapper):

```powershell
$tel = "C:\Users\tianx\codes\isales-telephony"
$env:Path = "C:\Program Files\CMake\bin;" + $env:Path
$py = "$tel\.venv-3.12\Scripts\python.exe"
$pybindDir = "$tel\deploy\edge\windows\pybind\aliyun_artc_pywrap"
$buildDir = "$tel\build\pybind"

cmake -S $pybindDir -B $buildDir `
    "-DPython3_EXECUTABLE=$py" "-DCMAKE_BUILD_TYPE=Release"
cmake --build $buildDir --config Release --target aliyun_artc_pywrap
```

Output: `build\pybind\Release\aliyun_artc_pywrap.cp312-win_amd64.pyd`
(258 KB). The ABI-tag suffix `.cp312-win_amd64` is pybind11's default
naming (Python import system accepts both bare and tagged form).
`build.ps1` step 4d was fixed 2026-05-17 (commit `b9cd5de`) to glob
`aliyun_artc_pywrap*.pyd` instead of the hardcoded bare name.

Built .pyd in dev location: `deploy\edge\windows\pybind\aliyun_artc_pywrap\aliyun_artc_pywrap.cp312-win_amd64.pyd`.
For runtime imports the 5 vendor DLLs must be on the DLL search path —
`build.ps1` copies them next to the `.pyd` automatically; for ad-hoc
dev imports run from the pybind dir.

Import smoke (proves §9.2 — 6 expected symbols):

```powershell
cd C:\Users\tianx\codes\isales-telephony\deploy\edge\windows\pybind\aliyun_artc_pywrap
& C:\Users\tianx\codes\isales-telephony\.venv-3.12\Scripts\python.exe -c `
    "import sys; sys.path.insert(0, '.'); import aliyun_artc_pywrap as m; print(sorted(a for a in dir(m) if not a.startswith('_')))"
# expect: ['AliyunArtcError', 'AudioObserver', 'EngineHandle', 'EngineListener', 'FrameRingBuffer', 'PcmFrame']
```

The 6 exported symbols match `windows-artc-pybind11/design.md` Decision 2
verbatim — `AliyunArtcError` exception subclass + 5 pybind11 type
wrappers (one `EngineHandle` SDK wrapper class + 4 supporting value /
helper types).

**Still to do** (pybind §9.3-§9.5):

- §9.3 PyInstaller frozen-exe smoke: `build.ps1` step 5+ runs PyInstaller
  bundling against the pybind output; needs a clean Windows PC (or a
  fresh user account with no dev tools) to verify the frozen exe finds
  all DLLs (incl. VC Runtime msvcp140 / vcruntime140 / vcruntime140_1)
- §9.4 Real ARTC RTC join smoke: dev box uses `aliyun_artc_pywrap.EngineHandle`
  + AppId/AppKey to join a test channel and confirm
  `on_join_channel_result(code=0)` callback. **Needs an RTC client token
  signed with AppKey**, which is in `engine.env::ISALES_RTC_APP_KEY` on
  the ECS (not on dev box).
- §9.5 PCM push/pull smoke: §9.4 + push silence PCM → ECS engine pulls
  → reverse path; end-to-end delay P95 ≤ 50 ms.

## Hardware rig: SIMCom SIM7600G-H

The dev box has one **SIMCom SIM7600G-H 4G LTE Cat 4 USB GSM modem**
plugged in continuously (uses one front-panel USB-A port). See the
project-d1-hardware-rig memory for full reference.

| Role (stable) | Current COM (dynamic — re-enumerated each plug) | AT probe response |
|---|---|---|
| **HS-USB AT PORT** (MI_02) | `COM12` (as of 2026-05-17) | `OK` to `AT\r\n` |
| HS-USB Audio (MI_04, Class=Ports — **not** USB Audio Class) | `COM11` | PCM byte stream gated by `AT+CPCMREG=1` from AT port |
| HS-USB Diagnostics | `COM10` | (no response) |
| HS-USB Modem #2 | `COM9` | `OK` (legacy RAS-compat AT) |
| HS-USB NMEA | `COM8` | (no response) |

**COM numbers ARE dynamic across re-plugs** — pyserial code matches on
description string (`"Simcom HS-USB AT PORT"`, `"Simcom HS-USB Audio"`)
or sibling-by-USB-composite-serial, never on raw COM number. See
`isales_telephony/modem_controller/platforms/windows_serial.py::find_audio_serial_path`.

SIM card (inserted, verified 2026-05-17 spike):
- ICCID `89860125801649389212` → 联通 (China Unicom)
- CSQ `31, 99` → signal full bars (31/31)

SIMCom driver: `SIMCOM_Windows_USB_Drivers_V1.0.2.zip` extracted to
`C:\Program Files (x86)\SIMCOM_5G_Windows_Driver\win10\`. Setup.exe
**only extracts**, doesn't register; must additionally run
`pnputil /add-driver "$dir\*.inf" /install` (admin). See
`reference-simcom-driver-install` memory.

Live spike (non-destructive, validates §4.7-§4.12 contracts against
real hardware) — run any time to confirm modem health:

```powershell
cd C:\Users\tianx\codes\isales-telephony
.\.venv\Scripts\python.exe scripts\d1_poc\poc_serial_pcm_spike.py
```

Expected: 8 assertions pass (find_audio_serial_path → COM11,
SerialATClient init OK, AT+CPCMREG=? returns `(0-1)`, AT+CPCMREG=1
outside call returns ERROR → PcmEnableError, etc.). Last run
2026-05-17 23:00 CST all green (commit `aeff11e`).

## Cloud-edge gRPC smoke (Windows → ECS, verified 2026-05-17 23:25 CST)

The Windows dev box can reach ECS `121.89.85.150:50051` over plain
gRPC; JWT auth + bidi stream up; Heartbeat round-trips.

```powershell
cd C:\Users\tianx\codes\isales-telephony
.\.venv\Scripts\python.exe scripts\cloud_edge_smoke.py `
    --endpoint 121.89.85.150:50051 `
    --token-file .edge-token-test.jwt `
    --timeout 15
```

Token (`device_id=edge-01`, 365d TTL, minted on ECS 2026-05-17) sits at
`C:\Users\tianx\codes\isales-telephony\.edge-token-test.jwt`
(gitignored). Re-mint with the ECS recipe in `deploy/cloud/STATE.md`
§ Secrets if expired or rotating device IDs.

## What's NOT yet done (dev rig perspective)

| Gate | Blocking on | Owner |
|---|---|---|
| **isales-telephony-edge full daemon launch** | Edge entry-point not yet exercised in any session — needs full wiring of: COM12 AT + COM11 SerialPcm audio + ARTC pybind .pyd in dev import path + cloud-edge gRPC client + `.edge-token-test.jwt` env injection | Claude (no extra user input needed) |
| **pybind §9.4 real ARTC RTC join smoke** | Needs RTC client join token (signed with `ISALES_RTC_APP_KEY` from ECS engine.env) — either scp the AppKey down, or sign the token on ECS and scp it down | Easy: scp + write a small `scripts/pybind_rtc_join_smoke.py` |
| **pybind §9.5 real PCM push/pull P95** | Needs §9.4 first + engine side joining same channel + clock-aligned latency measure | Medium: requires ECS engine to be put into a "listen on test channel" mode, or write a separate Linux-side listener using the ARTC Linux Python wrapper |
| **PyInstaller frozen-exe smoke (§9.3)** | Needs `build.ps1` full run end-to-end (will succeed now that toolchain is in place) + a clean Win PC for the final unwrap-and-launch test | User: need clean PC, OR I can produce the zip and we test on dev box for partial coverage |
| **D1 §9.3 真拨号 13301035545** | All of the above + AI provider stack ready (engine side) + PG seed data (campaign + lead) + edge daemon dialing through cloud-edge gRPC | Joint MVP gate; multiple pieces |

## Bootstrap a new dev session — verify before asserting state

```powershell
# 1. Python 3.12 / 3.14 + CMake + MSVC on PATH (or at known paths)
py -3.12 --version          # expect: Python 3.12.10
py -3.14 --version          # expect: Python 3.14.4
cmake --version             # expect: cmake version 4.3.2 (require fresh PowerShell after CMake PATH update)
Test-Path "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC"
# expect: True

# 2. ARTC SDK Windows vendor + pybind .pyd built
cd C:\Users\tianx\codes\isales-telephony
Test-Path "deploy\edge\windows\vendor\aliyun-artc-windows\x64\Release\AliRTCSdk.lib"
Test-Path "deploy\edge\windows\pybind\aliyun_artc_pywrap\aliyun_artc_pywrap.cp312-win_amd64.pyd"
# expect: True / True

# 3. pybind import smoke (6 expected symbols)
cd deploy\edge\windows\pybind\aliyun_artc_pywrap
& C:\Users\tianx\codes\isales-telephony\.venv-3.12\Scripts\python.exe -c `
    "import sys; sys.path.insert(0, '.'); import aliyun_artc_pywrap as m; print(sorted(a for a in dir(m) if not a.startswith('_')))"
# expect: ['AliyunArtcError', 'AudioObserver', 'EngineHandle', 'EngineListener', 'FrameRingBuffer', 'PcmFrame']

# 4. SIM7600G-H modem still on COM12 (AT) + COM11 (audio)
& C:\Users\tianx\codes\isales-telephony\.venv\Scripts\python.exe -c `
    "import serial.tools.list_ports as lp; [print(p.device, p.description) for p in lp.comports() if p.vid == 0x1e0e]"
# expect: 5 COM lines, descriptions include 'AT PORT' and 'Audio'

# 5. Cloud-edge gRPC smoke from this box (proves end-to-end auth + stream)
cd C:\Users\tianx\codes\isales-telephony
Test-Path .edge-token-test.jwt
# expect: True (re-mint via ECS if missing — see deploy/cloud/STATE.md § EDGE_DEVICE_TOKEN)
.\.venv\Scripts\python.exe scripts\cloud_edge_smoke.py `
    --endpoint 121.89.85.150:50051 --token-file .edge-token-test.jwt --timeout 15
# expect: '==> CONNECTED' + '==> Heartbeat sent OK' + '==> done'
```

If ANY of the 5 fails, that is ground truth — report it and dig in.
**Never** infer Windows rig state from OpenSpec tasks.md checkboxes
alone. See [[feedback-ground-truth-before-pending]] for past regressions
and the prevention rule.

## Related state files

- `isales/deploy/cloud/STATE.md` — ECS side (4 services, 50051 listen,
  PG schema, PAT credentials, smoke-from-edge recipe)
- `isales/CLAUDE.md` § OpenSpec workflow — establishes STATE.md as
  ground truth canonical when RUNBOOK / OpenSpec disagree
- `isales/openspec/changes/archive/2026-05-17-windows-client-core/acceptance.md`
  — D1 archive; § "Out-of-scope deferred items" lists §9 joint MVP gates
- `isales-telephony/scripts/d1_poc/WINDOWS_SETUP.md` — user-facing
  install recipe for the toolchain documented here
- `isales-telephony/scripts/d1_poc/poc_serial_pcm_spike.py` — hardware
  validation script for SerialPcm-over-COM path
- `isales-telephony/scripts/cloud_edge_smoke.py` — cloud-edge gRPC smoke
