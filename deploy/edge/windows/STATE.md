# Windows edge dev rig — current state snapshot

> **2026-05-31(晚)— modem 上下行【两个方向都实测可用】;"电话到AI"真凶在
> edge 上行链路,不在 modem / 不在驱动 / 不需要换 WinUSB。**
> - ✅ 上行(AI→电话,主机写 COM11):对端真人听到 tone。
> - ✅ 下行(电话→AI,主机读 COM11):读出干净 **8kHz** PCM,对端真人声 amp
>   2000–3500、静音 ~10(`capture_downlink.py` 实测)。
> - ❌ **仍未解的真 bug**:全栈真拨,引擎**全程只收到静音 → 因静音挂断**
>   (call_record #96 transcript 实证:greeting 发出=下行通,之后全是
>   `silence_activation`→`silence_max_reached`)。即 **modem 读得到对端声音,
>   但送不进引擎**。
> - **✅ 已锁定(call #98 fake-WAV 注入实测,见下方详节):** 用
>   `ISALES_FAKE_CAPTURE_WAV` 绕开 modem、把已知真人语音当上行喂进 daemon,
>   `upstream_push` 一路在推真语音、无 RtcError,**引擎照样只收到静音**。→ bug
>   **不在 modem、不在上行 pump**(上行 pump 工作,旧"pump 一抛错就死"假设证伪),
>   **在 `push_external_audio`→DingRTC 上行 publish→引擎接收**这一段(edge push
>   返回成功但音频没成为引擎可听的已发布流)。下一步在 engine/DingRTC publish 侧查。
> - **次要 bug:** 引擎挂断后 edge 没收到挂断、继续空推 + modem 通话不挂(烧钱)。
> - **已被本轮证伪的旧结论(别再信):**(a)"写共享句柄→下行读塌到 640 B/s 的
>   全双工硬墙"——`diag_duplex_collapse.py` 干净复测下行没塌;(b)"downlink
>   ≈32KB/s 偏 16kHz"——实测 8kHz;(c)"出路 A 换 WinUSB/Zadig、出路 B 改
>   Linux"——前提已塌,**Zadig 永久作废**(客户 PC 不可能换驱动)。
> - 详见下方 § "上行硬墙证伪 + 端点确证 + 下行真凶定位 (2026-05-31 晚)"。
> - COM 号每次重插重排(认 description,`discover_modem_serial_paths()`);
>   当前 AT=COM12 / AUDIO=COM11 / DIAG=COM10。

## ⏭️ 会话交接 — 2026-05-31 收尾,明天续（可能换电脑,故记于此而非本机 memory）

**结论(已坐实,别回头质疑):** "电话→AI 没通" 的 bug **锁定在
`WindowsDingRtcSession.push_external_audio` → DingRTC 上行 publish → 引擎接收**
这一段。证据:fake-WAV 注入(`ISALES_FAKE_CAPTURE_WAV` 绕开 modem 喂已知真人语音),
`upstream_push` 一路推真语音、无 RtcError,**引擎仍只收静音**(call_record #98:
`greeting→silence_activation×2→silence_max_reached`)。**不是 modem / 采集 /
上行 pump / 重采样 / 驱动**(全已逐一排除)。详见下方 § "上行硬墙证伪 + 端点确证
+ 下行真凶定位" 的 "决定性定位" 段。

**本会话提交(isales-telephony main,已 push):** `fd74cdf`(清过期误导结论)、
`a374a7f`(测试操作手册+脚本清单+`_run_daemon_dev.py`)、`e29e43e`(fake-WAV 钩子
+ 锁定 bug)。

**明天下一步(engine / DingRTC publish 侧 — edge 这边已查到头):**
1. 云端 `isales-engine` 查 audio observer 收没收到 edge-<callid> 的帧:**收到**=
   解码/VAD 问题;**没收到**=publish/subscribe 没接上。
2. 对照云端 Linux `isales_engine/transport/dingrtc.py::DingRtcSession` 与边缘
   `isales_telephony/audio_bridge/windows_dingrtc_session.py::join` 的 publish 配置链
   (`enable_custom_audio_capture(True)`+`set_external_audio_source(True,16000,1)`+
   `publish_local_audio_stream(True)`+`push_external_audio`)找差异——疑 Windows
   binding 的外部音源没真接到已发布 RTP 流。
3. 确认引擎 join 同信道 + `subscribe_all_remote_audio_streams`。
4. **次要 bug**:引擎挂断后 edge 没收到挂断、空推 2.5min、modem 通话不挂(烧钱)——
   查 gRPC `CallEvent(remote_hangup)` / RTC bye 的边缘传播。

**怎么续(全栈真拨复跑):** 见下方 § "音频诊断脚本清单 + 全栈真拨测试操作手册"。
要点速记:daemon 用 `.venv-3.12 python _run_daemon_dev.py`(`main_windows` 缺
DingRTC DLL wiring,直接 `-m` 会崩);测前复位 `device 3→idle` + `lead 2→new`;
campaign start 用 ECS heredoc 签 admin token(`$$` 在远程 `$(...)` 会被当 PID,
必须 heredoc);测上行用 `ISALES_FAKE_CAPTURE_WAV=<8k mono wav>`,接电话不用说话。

**换新电脑须先装好:** Python 3.12 + `.venv-3.12`(全运行依赖)+ DingRTC Windows
SDK vendor + `dingrtc_pywrap` .pyd 构建 + SIM7600 驱动 + `isales-4.pem` —— 见本文
下方各节 + § "Bootstrap a new dev session"。`downlink_8k.wav`(fake-WAV 源)是本机
临时产物,新机需用 `capture_downlink.py` 真拨重采一段,或换任意 8k/mono/int16 语音 WAV。

**Last updated**: 2026-05-30 — DingRTC Windows SDK 3.9.0 vendored at
`~/codes/vendor/DingRTC_Windows_SDK_3_9_0/` (sha256 `F594...19974`
matches mac/ECS); `dingrtc_pywrap` binding built + import + JOIN smoke
+ push/drain + Windows↔ECS dual-peer e2e all GREEN (`engine-rtc-dingrtc-
migration` §7.1-7.8). § 7.11 (this commit) physically removed
`vendor/aliyun-artc-windows/` + `pybind/aliyun_artc_pywrap/` +
`audio_bridge/windows_rtc_session.py` + ARTC tests/scripts;
`audio_bridge.get_default_rtc_session_factory(app_id=...)` now routes
Windows to `WindowsDingRtcSession`. § 7.9 (`build.ps1` integration +
PyInstaller DLL co-location) + § 7.10 (frozen-exe smoke) are the only
remaining § 7 subtasks. Toolchain (Python 3.12 + CMake + VS BuildTools)
unchanged; SIM7600G-H modem on COM12 (AT) / COM11 (audio); cloud-edge
gRPC smoke to ECS green.

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

**§7.8 dual-peer e2e GREEN (2026-05-30)** — Windows ↔ ECS DingRTC 跨网音频互通实测:

Setup:
- channel: `dual-peer-win-1780115083`, app_id: `o6dpsan9` (prod), 90s TTL
- Token mint via ECS `isales-engine-mint-rtc-token --user-id win-talker` (prod AppKey never leaves ECS)
- `scripts/windows_dingrtc_session_smoke.py` 扩展 `--app-id` / `--token` / `--gslb` 接受外签 token (与 demo path mutually exclusive)
- ECS listener: `ecs_pcm_loopback_listen.py --channel <same> --user-id ecs-listener --duration 25`

Evidence (各自 RTC stats):

| 端 | push | drain (inbound) | first frame | leave |
|---|---|---|---|---|
| Windows talker | 160 KB silence (5s @ 16kHz) | 404 frames / 775 KB | t+0s | clean exit 0 |
| ECS listener | 143 frames silence | 2498 frames / 4.6 MB | t+0.0006s | `{"ok":true}` |

意义:
- Windows push 在 ECS inbound 总量中确证 (Windows→ECS 上行通)
- ECS push 在 Windows drain 总量中确证 (ECS→Windows 下行通)
- 跨大陆 (Windows 本地 ↔ 阿里云 121.89.85.150) 跨网 RTC 数据面完整工作

ECS 端 `failed to open audio record/play device` 警告无害 — ECS 无声卡, external_audio_source +
observer 路径不依赖物理设备, vendor SDK noise warnings only.

13301035545 joint MVP gate **Windows §7 完整 GREEN**, RTC 数据面已不再阻塞.

Still to do (§7.9+):
- §7.9 build.ps1 集成 + DLL co-location
- §7.10 PyInstaller frozen-exe smoke
- §7.11 ARTC binding + vendor 清理 + `__init__.py` 路由切到
  WindowsDingRtcSession 默认 (currently 路由仍指 WindowsRtcSession,
  breaking-change 留 §7.8 已 green, 路由可在 §7.11 一并切)

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

## SIM7600 USB audio uplink — 实测可用 (2026-05-31)

**结论:USB 音频上行(主机→modem→对端 GSM)在 Windows 下工作。** 干净
重启后,通话中 `AT+CPCMREG=1`,往音频口写 8kHz/16bit/mono、320B/20ms
paced PCM,**对端真人听到 1kHz tone**(`retest_audio_write.py` 三变体
rtscts / dsrdtr+DTR-RTS / 单线程 lockstep 全 ok≈149、零 timeout;线状态
通话中 cts/dsr/cd 全 True)。流控不是关键变量。

**失败模式(曾误判为"写不进"):** modem 音频 **OUT 端点会进卡死态** ——
写全 `SerialTimeoutException`(ok=0),或阻塞写 `write_timeout=None` 第一帧
永久挂死;且**跨通话、跨 `AT+CPCMREG=0/1` 都清不掉**,只有整机重启
(物理重插已验证;`AT+CRESET` 软重启**待验证**)复位。疑似诱因(均非正常
操作):① `AT+CPCMFRM=1`(16K)与 8K 数据错配;② 阻塞写挂死留未决
overlapped WriteFile;③ **`AT+CFTRANRX` 灌大文件直接把整机 modem 搞挂**
(所有 AT 口失联,软恢复全失效,只能物理重插)。

**诊断旁证:** downlink 读音频口 **= 16000 B/s = 8kHz**(2026-05-31 晚干净复测,
`capture_downlink.py`;早先"≈32KB/s 偏16kHz"是卡死态/误测),8K uplink 写
对端能听到;模块自带 `C:` ~8.4MB/~1.8MB 可用(`AT+FSMEM`)。

**生产待办(→ OpenSpec change):**
- `isales_telephony/modem_controller/audio/windows_serial_pcm.py` 当前
  `write_chunk` **静默吞掉 `SerialTimeoutException`** —— 应改为:持续写超时
  = "OUT 卡死"信号 → 触发 modem 软复位(`AT+CRESET`/`AT+CFUN`)而非假装无事
  (踩中 root CLAUDE.md "多层 fallback / 静默兜底是坏味道")。
- 边缘 daemon 会话初应从**干净 modem 状态**起步;验证软复位能否替代物理重插。
- 严格 8K paced 写;禁用阻塞 `write_timeout=None`;音频口禁写错格式;禁
  `AT+CFTRANRX` 大文件。
- **备选(文件式,非实时)** `AT+CCMXPLAYWAV="C:/x.wav",1` 送对端,本固件支持
  (`=? → (1-2),(0-255)`),仅适合静态开场白/IVR;上传 `CFTRANRX` 有搞挂风险。

**✅ 上行写口已修复并真拨验证(2026-05-31):**`main_windows.py` +
`edge/main.py` 的 playback 已改为写 **Audio 口** + `adopt_serial_from(capture)`
共享句柄;用生产类 `validate_shared_handle_dial.py` 真拨 13301035545 实测
`cap._serial is pb._serial=True`、并发读写、**对端听到 tone**。另:COM 口已
改 **USB 描述符自动发现**(`discover_modem_serial_paths()`,无 env COM 号兜底,
发现失败 fail-loud),真机实测返回 AT=COM16/AUDIO=COM17;bridge 重采样改
rate-aware(用 `frame.sample_rate`,非写死 48kHz)。详见 OpenSpec change
`edge-modem-audio-out-recovery` §7/§8/§9。以下为修复前的根因记录:

**⚠️ 上行写口纠正(2026-05-31,影响真拨号):上行 PCM MUST 写 Audio 口
(COM11/MI_04),NOT Diagnostics 口(COM10)。** `tone_listen_test` 实测:写
COM10 / COM9 对端**两段全无声**(串口吞字节但不进通话);写 COM11/Audio
对端**听到 tone**。Audio 口是**全双工**——读(downlink)与写(uplink)走同
一口。`main_windows.py` 现把 `playback` 路由到 `ISALES_MODEM_PCM_WRITE_SERIAL_PATH`
默认 `COM10`(基于"COM11 只读"的错误理论,该理论源自卡死态下 COM11 写被堵
→ 误判),**带此配置真拨号 AI 语音进不了电话**。`edge/main.py::_build_audio_backends`
的 windows 分支是对的(capture+playback 同一 `audio_path`)。

**双开冲突 → 必须共享句柄:** Windows COM 口独占,capture 已占 COM11,
playback 不能再开 COM11(故当初绕道 COM10)。正解:capture 与 playback
**共享同一个 pyserial 句柄**(单 handle 并发读+写,overlapped IO 支持;
diag Phase D 实测单句柄并发读写可行)。`windows_serial_pcm.py` 的
`_SerialPortHolder` 设计意图即共享,但当前构造为两个独立实例未真正共享 →
需补共享句柄 wiring(随 `edge-modem-audio-out-recovery` OpenSpec change 一并修)。

**集成侧已发现的硬件约束(WIP 代码,待并入正式文档):**
- `audio_io.py::run_playback_pump`:**写不足整帧(<320B)会让 SIM7600 USB
  CDC 端点无限阻塞** → 必须累积到 320B(20ms@8kHz)整帧再写(已实现)。
- `bridge.py::_downstream_loop`:DingRTC `POSITION_PLAYBACK` 入站帧 **uid 为
  空** → 2 人房间里 uid 空时也应收(已实现)。
- `at_client.py::cpcmreg_enable`:CPCMREG=1 失败 → CPCMREG=0 → 重试(已实现)。
- `orchestrator.py`:teardown self-await 死锁修复(跳过 current task,已实现)。
- 这些 WIP **截至 2026-05-31 仍未提交**(working tree dirty;reflog 末条是
  `reset: moving to HEAD`,疑似 staged 后被 reset)。

诊断脚本(留在 isales-telephony 仓根):`retest_audio_write.py`
(证明上行可用 + 线状态,三变体写)、`diag_com11_audio.py`(读/写×端口
四方向探针)。其余一次性脚本(echo / probe / tone-listen / CCMXPLAYWAV
play_wav)已删,其结论已并入本节;CCMXPLAYWAV 备选与 `CFTRANRX` 上传坑
见上文,如需重建按描述即可。

## ~~SIM7600 USB audio 全双工 — Windows 共享句柄硬墙~~（硬墙说已证伪，2026-05-31 晚）

> **⚠️ 本节标题里的"硬墙"结论已被推翻 —— 见下一节 § "上行硬墙证伪…"。**
> 本节只保留两条仍成立的 ✅ 实测(AT 控制面修复 + 下行 AI 开场白通)作脉络;
> ❌ 的"写就塌下行 / 只能 Zadig 或 Linux"已废。

**真拨 13301035545 端到端实测里程碑(下半段"硬墙"已废)。**

**✅ 已通(实测)：**
- AT 控制面可靠性修复：`pyserial-asyncio` 在 Windows ProactorEventLoop 下间歇
  丢 modem 响应(~80% daemon 启动崩 init / 通话 URC 丢)；改用**裸 pyserial +
  专用读线程**(`modem_controller/platforms/windows_serial_at.py::open_threaded_serial`,
  `at_client.create_from_tty` Windows 分支)→ init 稳定、GMM 正确识别 SIM7600。
- **下行(引擎 AI → 对端)完全通：真人电话里听到 AI 开场白**
  ("您好，我是智联招聘的小雨…")。modem 自动发现 + gRPC + 派发 + RTC join +
  下行音频全链路工作。

**⚠️ 当初判的"上行硬墙"在干净 modem 上证伪(2026-05-31 晚,见下方新节)。**
旧记录(保留作脉络):曾观察到引擎全程静音 + 怀疑"写共享句柄→下行读塌到
640 B/s",据此推出 Zadig/改Linux 两条出路。**这两条出路及其前提均已作废**
—— 真凶不在 modem 传输层,而在旧并发双线程 pump + 卡死态 + 旧 build 误用
`MacosRtcSession`。下方新节用裸 modem 复现实测推翻了硬墙说。

## 上行"硬墙"证伪 + 端点确证 + 下行真凶定位 (2026-05-31 晚)

**触发:** 用户报"AI→电话通了,电话→AI 没通",要求查 USB 接口端点 + 复测上下行。

**1. MI_04 端点确证(pyusb 只读枚举,未换驱动):** 音频接口 MI_04 有
**三个端点**:`0x88 BULK IN`(下行)、`0x05 BULK OUT`(上行)、`0x89 INTR IN`
(串口通知)。**上下行是两根物理独立的 bulk 管子**,结构同 MI_01/02/03/05。
工具:`enum_mi04_endpoints.py`(仓根)。
→ **但这不构成换 WinUSB/Zadig 的理由:**(a) 一座位一台客户 PC,不可能让
客户跑 Zadig 换驱动 + 换完 MI_04 不再是 COM 口、整条 pyserial 链路全废 ——
**产品上是死路**;(b) 下面证明当前 usbser 驱动 + lockstep 已经够用,**根本
不需要全双工换驱动**。Zadig 路线**永久作废**。

**2. "写就塌下行"硬墙证伪:** `diag_duplex_collapse.py` 一通电话两段对比
(对端全程说话):
- P1 纯读 6s:16000 B/s,amp 1429–4707。
- P2 读+**每帧写静音** 6s(完全复刻 `run_duplex_pump`):**13120–19200 B/s,
  amp 1250–3347 —— 下行没塌。** 旧"塌到 640 B/s"未复现。
→ 干净 modem + lockstep 顺序读写下,下行读**扛得住并发写**。硬墙是卡死态 +
旧并发双线程 pump 的产物,不是物理极限。

**3. 下行(电话→AI)真凶 = daemon 上层,非 modem:**
- modem 层:`capture_downlink.py` 真拨实测,下行**稳定 8kHz、对端真人声
  amp 2000–3500**(静音时 ~10)—— modem 读这层完全正常。
- **引擎侧铁证(call_record #96,2026-05-31 22:03 真·全栈拨号):** transcript =
  `greeting`(开场白发出=下行通)→ `silence_activation`×2 →
  `hangup: silence_max_reached`。**引擎全程只收到静音**,即 modem 读得到的对端
  声音**没送进引擎**。这坐实"电话→AI 不通"发生在 modem 之上的 edge 上行/RTC,
  与 modem 无关。
- edge 代码层:capture→upstream ring→`_up_resampler`(scipy resample_poly
  8k→16k)→`push_audio`→DingRTC,整条 wiring 与重采样都对(代码读过,无明显 bug)。
- **【2026-05-31 23:19 全栈真拨 call_record #97 实测 — 头号假设证伪,上行 pump
  在当前 daemon【工作】】** daemon 日志:`dingrtc_join_result rc=0 channel=97
  uid=edge-97`(`WindowsDingRtcSession`,非 Macos)→ `audio_bridge_joined` →
  **`upstream_push n=1000 bytes=640000`**(上行一路推了 640KB)+ `duplex_pump
  n=1000` + `downstream_frame uid=engine-97 rate=48000`,**全程无
  `audio_bridge_upstream_unexpected_error`、无 RtcError**。即原"上行 pump 一抛
  RtcError 整通退出"(旧头号候选 a)**在当前正确接好的 daemon 没发生**,作废。
- **但 call #97 transcript 仍 = `greeting`→`silence_activation`×2→
  `silence_max_reached`(引擎仍静音)。⚠️ 关键 caveat:这通用户在打字、大概率
  没对电话出声**,所以引擎收静音很可能"本来就没人说话",非 bug。**此通对"真人声
  能否到引擎"既不证实也不证伪。**
- **dev 启动缺口(真实 bug,记下):** daemon 直接 `python -m
  isales_telephony.main_windows` 会 **`dingrtc_pywrap DLL load failed` 崩**——
  main_windows **没 wire DingRTC vendor DLL 目录**(production frozen exe 靠
  co-locate 规避)。dev 跑必须用 `_run_daemon_dev.py`(加了
  `os.add_dll_directory(vendor/lib/x64)`)。要么长期补 main_windows 的 DLL wiring。
- **残余候选(仅当"真说话仍静音"才查):** ① 推上去的是不是静音 ——
  `run_duplex_pump` **当前不打振幅**,看不出采到有没有人声 → 需补 amp 日志(或信
  `capture_downlink.py` 已证模组采得到人声);② RTC→引擎解码端格式/采样率;
  ③ 引擎是否真订阅 edge 流。

**✅ 决定性定位 — fake-WAV 注入实测(call_record #98,2026-05-31 23:31):**
绕开 modem、用 `ISALES_FAKE_CAPTURE_WAV` 把已知**真人语音**(`downlink_8k.wav`,
amp 2000-3500)当上行喂进 daemon。日志:`dingrtc_join_result rc=0 channel=98` +
**`upstream_push` 从 23:31:01 起一路推真语音**(引擎首次 `silence_activation`
在 +13.5s,之前已推十几秒语音)。**但 call #98 transcript 仍 =
`greeting→silence_activation×2→silence_max_reached`,引擎照样只收到静音。**
→ **bug 位置彻底锁定:不是 modem 采集(已旁路)、不是上行 pump(在推、无
RtcError)、而是 `WindowsDingRtcSession.push_external_audio` → DingRTC 上行
publish → 引擎接收这一段。** edge 侧 `push_audio` 返回成功(`upstream_push` 在
AwaIT 成功后才计数,无 RtcError),但推的音频没成为引擎可听的已发布流。
- **下一步(engine/DingRTC 侧):** ① engine 侧查有没有收到 edge-98 的 audio
  observer 帧(收到=解码/VAD 问题;没收到=publish/subscribe 没接上);② 复核
  edge join 的 publish 配置链 `enable_custom_audio_capture(True)` +
  `set_external_audio_source(True,16000,1)` + `publish_local_audio_stream(True)` +
  `push_external_audio` 是否真把外部源接到已发布 RTP 流(对照 cloud Linux
  `DingRtcSession` 与 dual-peer smoke 的差异);③ 确认引擎 join 同信道且
  `subscribe_all_remote_audio_streams`。
- **次要 bug(同测发现):** 引擎 21s 挂断 call #98 后,**edge 没收到挂断**、继续
  往死信道空推 2.5 分钟、modem 通话还连着(烧钱)。挂断不从 engine 传播到 edge,
  需查 gRPC CallEvent(remote_hangup)/ RTC bye 的边缘处理。
- **复测工具:** `_run_daemon_dev.py` + `ISALES_FAKE_CAPTURE_WAV=<8k mono wav>`
  起 daemon(见 `windows_serial_pcm.py::WindowsSerialPcmCapture` 的 TEST-ONLY 钩子,
  问题修复后应连同钩子一起删)。

## 音频诊断脚本清单 + 全栈真拨测试操作手册（免得每个 session 重问）

当前真机 COM(认 description,**重插必变**,代码用 `discover_modem_serial_paths()`):
**AT=COM12 / AUDIO=COM11 / DIAG=COM10**。所有脚本用 `.venv-3.12` 跑
(`C:\Users\tianx\codes\isales-telephony\.venv-3.12\Scripts\python.exe`)。

### A. 单脚本诊断(不经云端,直接驱 modem)

| 脚本(仓根) | 作用 | 跑法 |
|---|---|---|
| `_probe_audio_cfg.py` | 非破坏性读 modem 音频路由/采样率/增益 AT 配置(CSDVC/CPCMFRM/CPCMBANDWIDTH…) | `python _probe_audio_cfg.py`(改端口在文件里) |
| `enum_mi04_endpoints.py` | pyusb 只读枚举 modem USB 端点(证 MI_04 有 IN+OUT 两根 bulk) | `python enum_mi04_endpoints.py`(需 `pip install pyusb libusb-package`) |
| `capture_downlink.py` | **测下行**:拨号→读 COM11→报字节率+振幅、存 8k/16k WAV。对端要说话 | `python capture_downlink.py --phone 13301035545 --at COM12 --audio COM11 --secs 10` |
| `diag_duplex_collapse.py` | 测"写会不会塌下行读":一通两段 P1纯读 vs P2读+写静音。对端全程说话 | `python diag_duplex_collapse.py --phone 13301035545 --at COM12 --audio COM11` |
| `retest_audio_write.py` / `diag_com11_audio.py` | (旧)上行写可用性 + 读写×端口四方向探针 | 见文件头 |

### B. 全栈真拨测试（modem→RTC→engine→AI 闭环,测"电话→AI")

**目的:** 验证对端真人声经 edge 上行送进引擎、AI 能听懂。判读 = call_record
transcript 里出现非空 `text`/AI 回应你说的内容(通);仍 `silence_activation`(不通)。

1. **spot-check modem 健康**(`_probe_audio_cfg.py` 看 AT OK、CSQ 有信号)。ECS
   4 服务 active:`ssh -i isales-4.pem root@121.89.85.150 "systemctl is-active isales-api isales-engine isales-scheduler isales-worker"`。
2. **起 daemon**(headless,**必须用 launcher**——直接 `-m main_windows` 会因
   DingRTC DLL 未 wire 而崩):
   ```
   cd C:\Users\tianx\codes\isales-telephony
   .\.venv-3.12\Scripts\python.exe -u _run_daemon_dev.py > edge_daemon_test.log 2>&1
   ```
   等日志出 4 个 READY:`modem_registered` / `audio_port_opened` /
   `dingrtc_loaded` / `grpc_connected`。(后台跑时别用会退出的 wrapper 套
   `&`——wrapper 一退 daemon 被 SIGHUP 带走;直接让 python 进程做后台任务。
   重启前先杀干净旧 daemon,否则新 daemon 抢不到 COM 口报 "modem wedged"。)
3. **复位测试数据**(测试任务 campaign=1 / lead=2=13301035545 / device=3):
   ```
   ssh -i isales-4.pem root@121.89.85.150 "sudo -u postgres psql -d isales -c \"UPDATE device SET status='idle' WHERE id=3; UPDATE lead SET status='new', next_call_at=NULL, retry_count=0, follow_up_count=0 WHERE id=2;\""
   ```
   (device 3 会卡 `dialing`——心跳不复位它,必须 SQL 改;否则 scheduler 不派发。)
4. **触发 campaign start**(ECS 上用 JWT secret 签临时 admin token,只在 ECS 内用):
   ```
   ssh -i isales-4.pem root@121.89.85.150 "cd /opt/isales/current/isales-api; set -a; . /etc/isales/env/api.env; set +a; TOKEN=\$(/opt/isales/current/venv/bin/python -c \"from isales_api.auth.jwt import sign_jwt; print(sign_jwt({'sub':'t','role':'admin'}))\"); curl -sS -w ' http=%{http_code}\n' -X POST http://127.0.0.1/api/campaigns/1/start -H \"Authorization: Bearer \$TOKEN\""
   ```
   (或 isales-web 后台点「启动测试任务」。无 manual-dial API,只能走 campaign 派发。)
5. **接 13301035545 + 全程对 AI 持续说话 ~15s**(关键变量——不说话引擎必然收静音,
   测了等于白测)。
6. **判读**:
   - daemon `edge_daemon_test.log` 上行签名:`upstream_push n=… bytes=…`(上行在推)
     / `audio_bridge_upstream_unexpected_error`(pump 死了)/ `capture_pump_eof`
     (采集秒退)/ join 的是 `WindowsDingRtcSession` 还是 `MacosRtcSession`。
   - 引擎侧:`ssh … "sudo -u postgres psql -d isales -tA -c \"SELECT transcript FROM call_record ORDER BY id DESC LIMIT 1;\""`,看有没有非空 `text`。

**已知陷阱:**(a)`main_windows` 缺 DingRTC DLL wiring → 用 `_run_daemon_dev.py`;
(b)孤儿 daemon 占 COM 口 → 重启前 `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | ? CommandLine -match '_run_daemon_dev' | % { Stop-Process $_.ProcessId -Force }`;
(c)prod DB 写 / 签 admin token 触发 campaign,Claude 自动模式分类器会拦——需用户当场授权或用户自己用 `!` 跑;(d)每通拨完 lead 2 状态会变,重测前重跑第 3 步复位。

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
| **D1 §9.3 真拨号 13301035545** | All of the above + AI provider stack ready (engine side) + PG seed data (campaign + lead) + edge daemon dialing through cloud-edge gRPC | Joint MVP gate; multiple pieces. **音频上行已 unblock**(2026-05-31 对端听到 tone,见 § "SIM7600 USB audio uplink — 实测可用");剩 OUT-卡死软复位 + `windows_serial_pcm.py` 硬化 |

## Bootstrap a new dev session — verify before asserting state

```powershell
# 1. Python 3.12 / 3.14 + CMake + MSVC on PATH (or at known paths)
py -3.12 --version          # expect: Python 3.12.10
py -3.14 --version          # expect: Python 3.14.4
cmake --version             # expect: cmake version 4.3.2 (require fresh PowerShell after CMake PATH update)
Test-Path "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC"
# expect: True

# 2. DingRTC SDK Windows vendor (cross-repo) + pybind .pyd built
cd C:\Users\tianx\codes\isales-telephony
Test-Path "$env:USERPROFILE\codes\vendor\DingRTC_Windows_SDK_3_9_0\lib\x64\DingRTC.lib"
Test-Path "$env:USERPROFILE\codes\vendor\DingRTC_Windows_SDK_3_9_0\lib\x64\DingRTC.dll"
Test-Path "build\dingrtc-binding\Release\dingrtc_pywrap.cp312-win_amd64.pyd"
# expect: True / True / True

# 3. pybind import smoke (9 expected symbols)
$vendor = "$env:USERPROFILE\codes\vendor\DingRTC_Windows_SDK_3_9_0\lib\x64"
$pyd = "C:\Users\tianx\codes\isales-telephony\build\dingrtc-binding\Release"
& C:\Users\tianx\codes\isales-telephony\.venv-3.12\Scripts\python.exe -c `
    "import os, sys; os.add_dll_directory(r'$vendor'); sys.path.insert(0, r'$pyd'); import dingrtc_pywrap as m; print(sorted(a for a in dir(m) if not a.startswith('_')))"
# expect: ['AudioObserver', 'DingRtcError', 'EngineHandle', 'EngineListener', 'FrameRingBuffer', 'PcmFrame', 'POSITION_PLAYBACK', 'POSITION_REMOTE_USER', 'set_log_dir_path']

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
