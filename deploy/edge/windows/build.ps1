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
$VendorDir = Join-Path $PSScriptRoot "vendor\aliyun-artc-windows"
$IconDir = Join-Path $PSScriptRoot "icons"
$PybindDir = Join-Path $PSScriptRoot "pybind\aliyun_artc_pywrap"
$PybindBuildDir = Join-Path $RepoRoot "build\pybind"
$DistRoot = Join-Path $RepoRoot "dist"
$DistApp = Join-Path $DistRoot "isales-telephony"
$Version = (Get-Date -Format "yyyyMMdd-HHmmss")

Write-Host "iSales Windows edge build" -ForegroundColor Cyan
Write-Host "Repo:     $RepoRoot"
Write-Host "Vendor:   $VendorDir"
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

# 3. Warn if the operator hasn't dropped the ARTC SDK in.
if (-not (Test-Path $VendorDir)) {
    Write-Host "WARNING: $VendorDir does not exist — the build will succeed " `
        "but the resulting exe cannot join a real RTC channel. Drop the " `
        "Aliyun ARTC-for-Windows SDK there before producing a release build." `
        -ForegroundColor Yellow
} elseif (-not (Get-ChildItem -Path $VendorDir -Filter "*.dll" -Recurse)) {
    Write-Host "WARNING: $VendorDir contains no DLLs — same caveat as above." `
        -ForegroundColor Yellow
}

# 4. Build the aliyun_artc_pywrap pybind11 binding (CMake).
#
# Spec: openspec/changes/windows-artc-pybind11/specs/deployment-topology/
# § Requirement: Windows ARTC SDK pybind11 binding 构建产物.
$ArtcLib = Join-Path $VendorDir "x64\Release\AliRTCSdk.lib"
if (-not (Test-Path $ArtcLib)) {
    Write-Host "WARNING: $ArtcLib missing — skipping pybind11 binding build." `
        -ForegroundColor Yellow
    Write-Host "         frozen exe will fail to import aliyun_artc_pywrap;" `
        -ForegroundColor Yellow
    Write-Host "         RTC join will not work. This is fine for a smoke" `
        -ForegroundColor Yellow
    Write-Host "         build but NOT for a release. Drop the SDK and rerun." `
        -ForegroundColor Yellow
}
else {
    # 4a. Verify Python version is 3.12.x — must match the interpreter
    # PyInstaller will bundle.
    $PyVersion = & $Python -c "import sys; print('%d.%d.%d' % sys.version_info[:3])"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to query Python version."
    }
    Write-Host "Python: $PyVersion"
    if ($PyVersion -notlike "3.12.*") {
        throw ("Python 3.12.x required (found $PyVersion). The aliyun_artc_pywrap " +
               "ABI must match the interpreter PyInstaller bundles into the frozen exe.")
    }

    # 4b. Verify CMake + MSVC build tools are present.
    $CMake = (Get-Command cmake -ErrorAction SilentlyContinue)
    if (-not $CMake) {
        throw ("cmake not on PATH. Install CMake 3.20+ + Visual Studio Build Tools " +
               "2022 (C++ workload, Windows SDK 10.0.22621). " +
               "See openspec/changes/windows-artc-pybind11/design.md Migration Plan.")
    }

    # 4c. Verify the pybind11 git submodule is checked out.
    if (-not (Test-Path (Join-Path $PybindDir "deps\pybind11\CMakeLists.txt"))) {
        throw ("pybind11 submodule not checked out at " +
               (Join-Path $PybindDir "deps\pybind11") +
               ". Run: git submodule update --init --recursive")
    }

    Write-Host "Building aliyun_artc_pywrap.pyd via CMake ..." -ForegroundColor Cyan
    try {
        & cmake -S $PybindDir -B $PybindBuildDir `
            "-DPython3_EXECUTABLE=$Python" `
            "-DCMAKE_BUILD_TYPE=Release"
        if ($LASTEXITCODE -ne 0) {
            throw "CMake configure failed (exit $LASTEXITCODE). Common causes:`n" +
                  "  - VS Build Tools 2022 not installed (need C++ workload)`n" +
                  "  - Windows SDK 10.0.22621 missing`n" +
                  "  - Python development headers missing (reinstall Python 3.12 with 'tcl/tk and IDLE' or use python.org installer)"
        }
        & cmake --build $PybindBuildDir --config Release --target aliyun_artc_pywrap
        if ($LASTEXITCODE -ne 0) {
            throw "CMake build failed (exit $LASTEXITCODE). Check build output above."
        }
    }
    catch {
        Write-Host $_ -ForegroundColor Red
        throw
    }

    # 4d. Copy .pyd into the pybind source dir so PyInstaller spec's
    # `binaries` glob picks it up. design.md Decision 6.
    #
    # pybind11 names the output with an ABI tag by default (e.g.
    # `aliyun_artc_pywrap.cp312-win_amd64.pyd`); Python's import system
    # accepts either the bare name or the ABI-tagged form. Glob so the
    # script doesn't break when the tag string changes (Py version,
    # arch).
    $BuiltPyd = Get-ChildItem -Path (Join-Path $PybindBuildDir "Release") `
        -Filter "aliyun_artc_pywrap*.pyd" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $BuiltPyd) {
        throw "CMake claimed success but no aliyun_artc_pywrap*.pyd found under $PybindBuildDir\Release."
    }
    Copy-Item -Path $BuiltPyd.FullName -Destination (Join-Path $PybindDir $BuiltPyd.Name) -Force

    # 4e. Smoke import — fail fast if Python ABI mismatch.
    Push-Location $PybindDir
    try {
        & $Python -c "import sys; sys.path.insert(0, '.'); import aliyun_artc_pywrap; print('aliyun_artc_pywrap version:', aliyun_artc_pywrap.__version__)"
        if ($LASTEXITCODE -ne 0) {
            throw "Smoke import of aliyun_artc_pywrap failed. Python ABI mismatch or missing VC Runtime."
        }
    }
    finally {
        Pop-Location
    }
}

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
