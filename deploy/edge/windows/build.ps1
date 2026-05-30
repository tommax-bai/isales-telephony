# Build the iSales Windows edge client.
#
# Spec: windows-client-core / tasks.md § 7.2.
#
# Usage:
#     pwsh -ExecutionPolicy Bypass -File deploy\edge\windows\build.ps1
#
# Outputs `dist\isales-telephony\` and `dist\isales-telephony-<version>.zip`
# at the repo root, ready to publish to OSS or hand to install.ps1.

#Requires -Version 5.1
$ErrorActionPreference = "Stop"

$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..\..").Path
$IconDir = Join-Path $PSScriptRoot "icons"
$DistRoot = Join-Path $RepoRoot "dist"
$DistApp = Join-Path $DistRoot "isales-telephony"
$Version = (Get-Date -Format "yyyyMMdd-HHmmss")

Write-Host "iSales Windows edge build" -ForegroundColor Cyan
Write-Host "Repo:     $RepoRoot"
Write-Host "Icon dir: $IconDir"
Write-Host "Output:   $DistApp"
Write-Host ""

# 1. Sanity checks.
if (-not (Test-Path (Join-Path $RepoRoot "pyproject.toml"))) {
    throw "pyproject.toml not found in $RepoRoot — run from isales-telephony repo root."
}

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "No .venv detected; creating one ..." -ForegroundColor Yellow
    python -m venv (Join-Path $RepoRoot ".venv")
}

# 2. Install deps.
Write-Host "Installing build deps ..." -ForegroundColor Cyan
& $Python -m pip install --upgrade pip
& $Python -m pip install pyinstaller
& $Python -m pip install -e "$RepoRoot[windows]"

# 3-4. TODO §7.9 (engine-rtc-dingrtc-migration): wire DingRTC pybind binding
#      build + vendor DLL co-location.
#
# 旧的 ARTC SDK + aliyun_artc_pywrap pybind 整段在 §7.11 (2026-05-30) 删除
# (commit ada5df5..c669afe). 替代:
#   - vendor SDK 路径 = $env:USERPROFILE\codes\vendor\DingRTC_Windows_SDK_3_9_0\
#     (cross-repo shared, gitignored, 不在 isales-telephony repo 内)
#   - pybind 源码    = $PSScriptRoot\pybind\dingrtc_pywrap\ (in repo)
#   - 当前 build.ps1 跳过 pybind build 步骤; cmake 手工触发, 见
#     isales-telephony/deploy/edge/windows/STATE.md § DingRTC binding 的
#     "Manual build invocation" PowerShell 段落.
#   - §7.9 将把 DingRTC vendor check + cmake -S pybind\dingrtc_pywrap +
#     copy DingRTC.dll + 12 个 runtime DLLs next-to-pyd 全部 wire 进这里.
#
# 当前路径 (§7.11 后): build.ps1 跑出来的 .exe 是无 RTC 的 smoke binary
# (modem + AT + cloud-edge gRPC 全部可用; RTC 离线). 真 RTC release build
# 等 §7.9 完成.

# 5. Run PyInstaller.
Write-Host "Running PyInstaller ..." -ForegroundColor Cyan
Push-Location $RepoRoot
try {
    & $Python -m PyInstaller `
        "deploy\edge\windows\isales-telephony.spec" `
        --noconfirm `
        --clean `
        --distpath "$DistRoot" `
        --workpath (Join-Path $DistRoot "build")
} finally {
    Pop-Location
}

# 6. Stamp the version.
$VersionFile = Join-Path $DistApp "VERSION.txt"
Set-Content -Path $VersionFile -Value $Version -Encoding UTF8

# 7. Zip up.
$ZipPath = Join-Path $DistRoot "isales-telephony-$Version.zip"
Write-Host "Compressing to $ZipPath ..." -ForegroundColor Cyan
if (Test-Path $ZipPath) {
    Remove-Item -Path $ZipPath -Force
}
Compress-Archive -Path $DistApp -DestinationPath $ZipPath -CompressionLevel Optimal

Write-Host ""
Write-Host "Build complete." -ForegroundColor Green
Write-Host "Frozen app: $DistApp"
Write-Host "Release zip: $ZipPath"
