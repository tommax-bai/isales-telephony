# Install / upgrade the iSales Windows edge client.
#
# Spec: windows-client-core / deployment-topology § Scenario "部署脚本".
#
# Usage (from inside the unzipped release directory):
#     pwsh -ExecutionPolicy Bypass -File install.ps1
#
# Optional flags:
#     -ZipPath <path>   — install from a zip rather than the local dir
#     -NoAutoStart      — register HKCU Run but don't launch the app now
#
# Idempotent: re-running upgrades an existing install in place, preserves
# %APPDATA%\isales\env\telephony.env (so the token survives upgrades).

#Requires -Version 5.1
[CmdletBinding()]
param(
    [string]$ZipPath,
    [switch]$NoAutoStart
)

$ErrorActionPreference = "Stop"

$AppName = "ISales"
$ProgramsDir = Join-Path $env:LOCALAPPDATA "Programs\isales"
$AppDir = $ProgramsDir
$ExeName = "isales-telephony.exe"
$ExePath = Join-Path $AppDir $ExeName
$AppDataDir = Join-Path $env:APPDATA "isales"
$EnvDir = Join-Path $AppDataDir "env"
$SqliteDir = Join-Path $AppDataDir "sqlite"
$LogsDir = Join-Path $AppDataDir "logs"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

Write-Host "iSales edge client installer" -ForegroundColor Cyan
Write-Host "Programs dir: $ProgramsDir"
Write-Host "AppData dir:  $AppDataDir"
Write-Host ""

# 1. AppData layout — created idempotently. Spec § "Windows 安装路径约定".
foreach ($dir in @($EnvDir, $SqliteDir, $LogsDir)) {
    if (-not (Test-Path $dir)) {
        Write-Host "Creating $dir"
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

# 2. Resolve source (zip vs current dir).
if ($ZipPath) {
    if (-not (Test-Path $ZipPath)) {
        throw "ZipPath $ZipPath does not exist."
    }
    $TempExtract = Join-Path $env:TEMP "isales-install-$(Get-Date -Format yyyyMMddHHmmss)"
    Write-Host "Extracting $ZipPath to $TempExtract ..." -ForegroundColor Cyan
    New-Item -ItemType Directory -Path $TempExtract -Force | Out-Null
    Expand-Archive -Path $ZipPath -DestinationPath $TempExtract -Force
    $SourceDir = Join-Path $TempExtract "isales-telephony"
    if (-not (Test-Path $SourceDir)) {
        throw "Expected $SourceDir inside zip — does the release artifact include the isales-telephony/ folder?"
    }
} else {
    $SourceDir = $PSScriptRoot
    if (-not (Test-Path (Join-Path $SourceDir $ExeName))) {
        throw "Could not find $ExeName next to install.ps1. Pass -ZipPath or run from the unzipped release dir."
    }
}

# 3. Copy app files. We do NOT touch %APPDATA%\isales\env\ — preserves the
# user's existing token across upgrades.
if (Test-Path $ProgramsDir) {
    Write-Host "Upgrading existing install at $ProgramsDir ..." -ForegroundColor Yellow
    # Stop the existing exe if running, otherwise the copy fails on
    # locked _internal\python*.dll.
    Get-Process -Name "isales-telephony" -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "Stopping running process pid=$($_.Id) ..."
        Stop-Process -Id $_.Id -Force
        Start-Sleep -Milliseconds 500
    }
    Remove-Item -Recurse -Force -Path $ProgramsDir
}
New-Item -ItemType Directory -Path $ProgramsDir -Force | Out-Null
Write-Host "Copying $SourceDir to $ProgramsDir ..." -ForegroundColor Cyan
Copy-Item -Path (Join-Path $SourceDir "*") -Destination $ProgramsDir -Recurse -Force

if (-not (Test-Path $ExePath)) {
    throw "Install failed — $ExePath missing after copy."
}

# 4. HKCU Run key. Spec § "Windows 进程托管（启动注册表 Run 项）".
Write-Host "Registering HKCU\Run\$AppName -> $ExePath ..." -ForegroundColor Cyan
if (-not (Test-Path $RunKey)) {
    New-Item -Path $RunKey -Force | Out-Null
}
Set-ItemProperty -Path $RunKey -Name $AppName -Value "`"$ExePath`""

# 5. Drop an env template if the user has no env file yet — gives them a
# starting point even before they paste the token via the dialog.
$EnvFile = Join-Path $EnvDir "telephony.env"
$EnvTemplate = Join-Path $ProgramsDir "env.example.txt"
if ((-not (Test-Path $EnvFile)) -and (Test-Path $EnvTemplate)) {
    Write-Host "Seeding $EnvFile from env.example.txt ..." -ForegroundColor Cyan
    Copy-Item -Path $EnvTemplate -Destination $EnvFile -Force
}

# 6. Launch (or skip when -NoAutoStart).
if ($NoAutoStart) {
    Write-Host "-NoAutoStart specified; not launching. Sign out + sign in or run $ExePath manually." `
        -ForegroundColor Yellow
} else {
    Write-Host "Launching $ExePath ..." -ForegroundColor Cyan
    Start-Process -FilePath $ExePath
}

Write-Host ""
Write-Host "Install complete." -ForegroundColor Green
Write-Host "If this is the first install, the activation dialog will appear shortly — paste the EDGE_DEVICE_TOKEN."
