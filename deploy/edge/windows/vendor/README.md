# Vendor binaries for the Windows edge client

This directory is **empty** post-`engine-rtc-dingrtc-migration` § 7.11
(2026-05-30) — RTC SDK and pybind11 binding both moved out of the repo:

| Asset | Where it lives now |
|---|---|
| DingRTC Windows SDK 3.9.0 (`DingRTC.dll`, 11 deps DLLs, headers, models) | `~/codes/vendor/DingRTC_Windows_SDK_3_9_0/` (cross-repo shared, gitignored, **outside** this repo). Symmetric with mac (`~/codes/vendor/DingRTC_macOS_SDK_3_9_0/`) + cloud Linux (`/opt/isales/vendor/DingRTC_Linux_SDK_3_9_0/`) per `engine-rtc-dingrtc-migration/design.md` D9. |
| pybind11 binding source | `deploy/edge/windows/pybind/dingrtc_pywrap/` (in repo) |
| pybind11 binding .pyd output | `build/dingrtc-binding/Release/dingrtc_pywrap.cp312-win_amd64.pyd` (gitignored build artifact) |

**Setup recipe**: download + extract the SDK zip:

```powershell
# One-time per Windows dev rig.
$VendorRoot = "$env:USERPROFILE\codes\vendor"
New-Item -ItemType Directory -Force -Path $VendorRoot | Out-Null
$Zip = "$VendorRoot\.download\DingRTC_Windows_SDK_3_9_0.zip"
$Url = "https://dingrtc.oss-cn-zhangjiakou.aliyuncs.com/sdk/windows/3.9.0/DingRTC_Windows_SDK_3_9_0.zip"
Invoke-WebRequest -Uri $Url -OutFile $Zip
Expand-Archive -Path $Zip -DestinationPath $VendorRoot -Force
# Verify: sha256 must match F594...19974 — see isales-telephony/deploy/edge/windows/STATE.md § DingRTC SDK
Get-FileHash -Algorithm SHA256 $Zip
```

**Why outside this repo?** vendor SDK is large (~48 MB zip, ~140 MB
extracted) and shared across smoke / dev / build sessions; keeping it
under `~/codes/vendor/` (a cross-repo well-known location) lets one
download serve every iSales repo that needs it (`isales-engine` cloud
binding builds against the same SDK family on Linux).

**§ 7.9 next step**: `build.ps1` will read this vendor location +
`isales-telephony.spec` will collect `lib/x64/*.dll` for the frozen exe.
Until § 7.9 is implemented, `build.ps1` skips the pybind build (smoke
binary, no RTC), and ad-hoc cmake invocation (see STATE.md
"Manual build invocation") produces the working `.pyd` directly.

For the binding source / smoke / state-of-the-build snapshot, see
[`../STATE.md` § DingRTC binding](../STATE.md).
