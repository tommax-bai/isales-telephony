# Windows edge dev rig — current state snapshot

**Last updated**: 2026-05-26 — pre-§7 toolchain state unchanged (Python
3.12 + CMake + VS BuildTools + SIM7600G-H + cloud-edge gRPC all still
green per 2026-05-17 snapshot). Vendor/binding paragraphs below
**rewritten to the post-§7 target** (DingRTC 3.9.0 + `dingrtc_pywrap`)
as the going-forward reference. **The Windows box has NOT yet run
§7** of `engine-rtc-dingrtc-migration` — `aliyun_artc_pywrap.pyd` is
still the built artifact there; the dev rig catches up when a Windows
session runs `build.ps1` against the new vendor layer.

This file is the Windows-dev-rig sibling of
`isales/deploy/cloud/STATE.md`. It records the state of the **build
environment + edge runtime + connected hardware** on the developer's
Windows machine. Reading this before reporting any
"Windows-side X is missing" prevents the regression class documented
in `[[feedback-ground-truth-before-pending]]` memory.

> **Fresh Claude Code session resuming work — READ THIS FIRST**. The
> OpenSpec tasks.md checkboxes for `engine-rtc-dingrtc-migration § 7`
> and `windows-artc-pybind11` lag the actual install + build state.
> This file is canonical for the dev box; CLAUDE.md (root) makes
> STATE.md files the arbiter when RUNBOOK / OpenSpec disagree. See
> § "Bootstrap a new dev session" at the bottom for the verification
> recipe.
>
> **§ 7 migration banner (2026-05-26).** The next Windows session
> needs to: (a) download `DingRTC_Windows_SDK_3_9_0.zip` to
> `~/codes/vendor/DingRTC_Windows_SDK_3_9_0/`, (b) rebuild the
> pybind11 binding under its new name `dingrtc_pywrap` against
> `DingRTC.h` (the iSales-engine Linux pybind binding has the
> reference shape — same `EngineHandle` API surface), (c) re-run the
> Bootstrap steps below. The legacy `aliyun_artc_pywrap.pyd` +
> `AliRTCSdk.dll` artefacts SHALL be removed once `dingrtc_pywrap.pyd`
> import-smokes green. Vendor source URL + sha256 are pinned in
> `isales/deploy/cloud/STATE.md` § "DingRTC SDK vendor".

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
| `C:\Users\tianx\codes\isales-telephony\.venv-3.12\` | 3.12.10 | **Pybind build venv only**. Used as `-DPython3_EXECUTABLE=...` for CMake configure; the produced `.pyd` ABI is tied to this Python. Pre-§7 = `aliyun_artc_pywrap.cp312-win_amd64.pyd`; post-§7 = `dingrtc_pywrap.cp312-win_amd64.pyd`. Not used for tests / dev. |

`.gitignore` covers `.venv*/` (widened 2026-05-17 commit `b9cd5de`) so
both venvs stay out of git.

## DingRTC SDK for Windows (vendored, gitignored) — post-§ 7 target

> **Pre-§ 7 reality check.** The dev box currently still has the
> legacy Aliyun ARTC artefacts at
> `deploy/edge/windows/vendor/aliyun-artc-windows/` (`AliRTCSdk.lib` +
> `AliRTCSdk.dll` + 4 sibling DLLs, from `AliVCSDK_ARTC-7.6.0.zip`
> downloaded via `alivc-demo-cms.alicdn.com`). They stay in place
> until §7.9 of `engine-rtc-dingrtc-migration` removes them.

Target location: `~/codes/vendor/DingRTC_Windows_SDK_3_9_0/` (mirrors
the macOS + cloud Linux DingRTC vendor layout — one tree per platform
under `~/codes/vendor/DingRTC_*_SDK_3_9_0/`, gitignored everywhere).

```
~/codes/vendor/DingRTC_Windows_SDK_3_9_0/
├── include/          # DingRTC C++ headers (DingRTC.h, RtcEngine.h,
│   │                 # RtcEngineInterface.h, RtcEngineTypes.h, ...)
│   └── ...
└── x64/
    └── Release/
        ├── DingRTC.lib         # link-time import lib for the .pyd
        ├── DingRTC.dll         ← runtime dependency
        ├── libffmpeg.dll       (vendor ffmpeg, replaces alivcffmpeg.dll)
        └── ... (vendor-shipped sibling DLLs per the SDK zip)
```

Vendor source / sha256 are pinned in `isales/deploy/cloud/STATE.md`
§ "DingRTC SDK vendor":

```
https://dingrtc.oss-cn-zhangjiakou.aliyuncs.com/sdk/windows/3.9.0/DingRTC_Windows_SDK_3_9_0.zip
sha256: f59482589a211e3fc4368c4750dfa50603f03c0acdf3d157728a41ac1ca19974
```

The legacy ApsaraVideo Live ARTC `AliVCSDK_ARTC-7.6.0` SDK is gone —
DingRTC 3.x is a separate product line (RTC PaaS new-generation) and
its tokens / channels are not interoperable with the old Live ARTC
SDK. See `isales/deploy/cloud/STATE.md` § "Migration from
ApsaraVideo Live ARTC SDK (legacy, gone)" for the cross-product
mismatch that caused the migration.

**MSVC 19.44 + `/permissive-`**: keep the `/permissive-` drop from
2026-05-17 commit `b9cd5de` until §7 confirms the DingRTC vendor
headers are strict-conformance-clean. If they are, re-enable
`/permissive-` in the new binding's CMakeLists; if not, document
which header trips C7626 here.

## pybind11 binding build (`dingrtc_pywrap`, post-§ 7)

> **Pre-§ 7 reality check.** The dev box still has the legacy
> `aliyun_artc_pywrap.cp312-win_amd64.pyd` (258 KB, built 2026-05-17,
> import smoke green for the legacy AliRTC API). The post-§ 7
> rebuild swaps the vendor layer (`AliRTCEngine` → `RtcEngine`,
> `AliRtcAuthInfo` → `RtcEngineAuthInfo`, `pushExternalAudioCapture`
> → `pushExternalAudioFrame`, etc.); the binding's framework code
> (`audio_observer.cpp` / `engine_listener.cpp` / `ring_buffer.cpp`
> / `CMakeLists.txt` skeleton) is reused verbatim because that
> framework is already DingRTC-agnostic, see § 7.4 of
> `engine-rtc-dingrtc-migration`.

Source (post-§ 7): `isales-telephony/deploy/edge/windows/pybind/dingrtc_pywrap/`

| File | Purpose |
|---|---|
| `CMakeLists.txt` | CMake project; `find_package(Python3 3.12 EXACT REQUIRED)`; `pybind11_add_module(dingrtc_pywrap MODULE src/*.cpp)` |
| `src/bindings.cpp` | Exposes `EngineHandle` class (SDK wrapper) + 5 pybind11 types; vendor calls switched to `RtcEngine::Create` / `JoinChannel(authInfo, userName)` / `PushExternalAudioFrame(RtcEngineAudioFrame)` per DingRTC 3.x API |
| `src/audio_observer.cpp` + `.h` | `AudioObserver : public RtcEngineAudioFrameObserver` — pushes inbound PCM frames to `FrameRingBuffer` (frame type swapped from `IAudioFrameObserver`) |
| `src/engine_listener.cpp` + `.h` | `EngineListener : public RtcEngineEventListener` — 4 setter-injected Python callbacks (join / leave / network / connection-state); base class swapped from `AliEngineEventListener` |
| `src/ring_buffer.cpp` + `.h` | `FrameRingBuffer` — mutex-protected MPSC, drop-oldest on overflow (unchanged) |
| `deps/pybind11/` | git submodule, pybind11 v3.0.4 (unchanged) |

Build invocation (manual, bypassing `build.ps1`'s frozen-exe wrapper):

```powershell
$tel = "C:\Users\tianx\codes\isales-telephony"
$env:Path = "C:\Program Files\CMake\bin;" + $env:Path
$py = "$tel\.venv-3.12\Scripts\python.exe"
$env:ISALES_DINGRTC_WINDOWS_SDK_PATH = "$env:USERPROFILE\codes\vendor\DingRTC_Windows_SDK_3_9_0"
$pybindDir = "$tel\deploy\edge\windows\pybind\dingrtc_pywrap"
$buildDir = "$tel\build\pybind"

cmake -S $pybindDir -B $buildDir `
    "-DPython3_EXECUTABLE=$py" "-DCMAKE_BUILD_TYPE=Release"
cmake --build $buildDir --config Release --target dingrtc_pywrap
```

Output: `build\pybind\Release\dingrtc_pywrap.cp312-win_amd64.pyd`.
The ABI-tag suffix `.cp312-win_amd64` is pybind11's default naming
(Python import system accepts both bare and tagged form). `build.ps1`
step 4d was fixed 2026-05-17 (commit `b9cd5de`) to glob `*_pywrap*.pyd`
— that glob covers the rename.

For runtime imports the vendor DLLs (`DingRTC.dll`, `libffmpeg.dll`,
+ any siblings the vendor zip ships) must be on the DLL search path —
`build.ps1` copies them next to the `.pyd` automatically; for ad-hoc
dev imports run from the pybind dir.

Import smoke (mirrors the pre-§ 7 6-symbol check):

```powershell
cd C:\Users\tianx\codes\isales-telephony\deploy\edge\windows\pybind\dingrtc_pywrap
& C:\Users\tianx\codes\isales-telephony\.venv-3.12\Scripts\python.exe -c `
    "import sys; sys.path.insert(0, '.'); import dingrtc_pywrap as m; print(sorted(a for a in dir(m) if not a.startswith('_')))"
# expect: ['AudioObserver', 'DingRtcError', 'EngineHandle', 'EngineListener', 'FrameRingBuffer', 'PcmFrame']
```

The exception class is renamed `AliyunArtcError` → `DingRtcError`
(matches `isales-engine/transport/dingrtc/_session.py` shape); the
other five pybind11 type wrappers keep their names because the
framework code is reused.

**Still to do** (post-§ 7 verification, tracked in
`engine-rtc-dingrtc-migration` §§ 7.7–7.11):

- §7.7 PoC demo appid `a4zfr1hn` join via `dingrtc_pywrap.EngineHandle`
  → `OnJoinChannelResult(code=0)`.
- §7.8 PoC real AppId `o6dpsan9` self-sign token (same `_pack_options`
  byte-perfect fix proven on cloud + macOS).
- §7.9 PyInstaller `build.ps1` `binaries` swap (`DingRTC.dll` +
  `dingrtc_pywrap.pyd`; remove `AliRTCSdk.dll` + `aliyun_artc_pywrap.pyd`)
  and `hiddenimports += ['dingrtc_pywrap']`.
- §7.10 frozen exe smoke on a clean Windows VM.
- §7.11 e2e: Windows edge + cloud engine same channel real互通 + dial
  13301035545.

**Frozen-exe `_internal/` expected contents** (post-§ 7.9):

```
_internal/
├── dingrtc_pywrap.cp312-win_amd64.pyd     ← swapped from aliyun_artc_pywrap
├── DingRTC.dll                            ← swapped from AliRTCSdk.dll
├── libffmpeg.dll                          ← swapped from alivcffmpeg.dll
├── ... (other vendor DLLs the SDK zip ships)
├── vcruntime140.dll
├── vcruntime140_1.dll
├── msvcp140.dll
└── (PyInstaller-bundled Python stdlib + iSales deps)
```

Verify after the next frozen-exe build by listing `_internal/` and
confirming no `Ali*` / `aliyun_artc_pywrap*` / `AliVCSDK*` artefacts
remain.

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
| **dingrtc-migration § 7 binding rebuild** | `dingrtc_pywrap` source not yet committed; needs `~/codes/vendor/DingRTC_Windows_SDK_3_9_0/` download + CMake rebuild + import smoke. Until done, the dev box keeps running the legacy `aliyun_artc_pywrap`. | Claude on next Windows session (no extra user input — vendor zip URL + sha256 pinned in cloud STATE.md) |
| **isales-telephony-edge full daemon launch** | Edge entry-point not yet exercised in any session — needs full wiring of: COM12 AT + COM11 SerialPcm audio + DingRTC pybind .pyd in dev import path + cloud-edge gRPC client + `.edge-token-test.jwt` env injection. Gated on § 7 binding rebuild. | Claude (no extra user input needed) |
| **dingrtc-migration § 7.7–7.8 DingRTC join smoke** | Needs DingRTC client join token. Demo path (`a4zfr1hn` + vendor demo-app-server) works without an AppKey; real-AppId path (`o6dpsan9` self-sign) needs `ISALES_RTC_APP_KEY` from ECS `engine.env`. Both already proven on cloud (§ 5) + macOS (§ 8). | Easy: `scripts/pybind_dingrtc_join_smoke.py` mirroring the macOS smoke script |
| **dingrtc-migration § 7.11 real PCM push/pull互通** | Needs § 7.7–7.8 first + engine-side joining same channel + clock-aligned latency measure. Cloud-side listener (`scripts/ecs_pcm_loopback_listen.py`) is already wired for DingRTC. | Medium: pair with a running ECS listener on a shared channel id |
| **PyInstaller frozen-exe smoke (§ 7.9–7.10)** | Needs `build.ps1` full run end-to-end with the DingRTC binaries swap (DingRTC.dll + dingrtc_pywrap.pyd) + a clean Win PC for the final unwrap-and-launch test | User: need clean PC, OR I can produce the zip and we test on dev box for partial coverage |
| **D1 真拨号 13301035545** | All of the above + AI provider stack ready (engine side) + PG seed data (campaign + lead) + edge daemon dialing through cloud-edge gRPC | Joint MVP gate; multiple pieces |

## Bootstrap a new dev session — verify before asserting state

```powershell
# 1. Python 3.12 / 3.14 + CMake + MSVC on PATH (or at known paths)
py -3.12 --version          # expect: Python 3.12.10
py -3.14 --version          # expect: Python 3.14.4
cmake --version             # expect: cmake version 4.3.2 (require fresh PowerShell after CMake PATH update)
Test-Path "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Tools\MSVC"
# expect: True

# 2. RTC SDK vendor + pybind .pyd built
#    Run the post-§7 block first; if it returns False/False, fall back to
#    the pre-§7 legacy block. Either path is acceptable evidence —
#    "neither" is the only outcome that means the rig is broken.
cd C:\Users\tianx\codes\isales-telephony

# 2a. post-§ 7 (DingRTC 3.9.0) — preferred
Test-Path "$env:USERPROFILE\codes\vendor\DingRTC_Windows_SDK_3_9_0\x64\Release\DingRTC.lib"
Test-Path "deploy\edge\windows\pybind\dingrtc_pywrap\dingrtc_pywrap.cp312-win_amd64.pyd"

# 2b. pre-§ 7 legacy (AliRTC) — still acceptable until § 7 lands
Test-Path "deploy\edge\windows\vendor\aliyun-artc-windows\x64\Release\AliRTCSdk.lib"
Test-Path "deploy\edge\windows\pybind\aliyun_artc_pywrap\aliyun_artc_pywrap.cp312-win_amd64.pyd"
# expect: 2a True/True OR 2b True/True. Both True is a transient
# during § 7.9 cleanup — fine, just complete § 7.9.

# 3. pybind import smoke (6 expected symbols)
# 3a. post-§ 7
cd deploy\edge\windows\pybind\dingrtc_pywrap
& C:\Users\tianx\codes\isales-telephony\.venv-3.12\Scripts\python.exe -c `
    "import sys; sys.path.insert(0, '.'); import dingrtc_pywrap as m; print(sorted(a for a in dir(m) if not a.startswith('_')))"
# expect: ['AudioObserver', 'DingRtcError', 'EngineHandle', 'EngineListener', 'FrameRingBuffer', 'PcmFrame']

# 3b. pre-§ 7 legacy
cd ..\aliyun_artc_pywrap
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
