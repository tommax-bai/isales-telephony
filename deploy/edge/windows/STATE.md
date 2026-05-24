# Windows edge dev rig — current state snapshot

**Last updated**: 2026-05-17 23:30 CST — Python 3.12 + CMake + VS BuildTools
installed; `aliyun_artc_pywrap.pyd` built + import smoke green;
SIM7600G-H modem on COM12 (AT) / COM11 (audio); cloud-edge gRPC smoke
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

## Aliyun ARTC SDK for Windows (vendored, gitignored)

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

## pybind11 binding build (`aliyun_artc_pywrap`)

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
