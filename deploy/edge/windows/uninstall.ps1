# Uninstall the iSales Windows edge client.
#
# Spec: windows-client-core / deployment-topology § Scenario "部署脚本"
#   ("卸载脚本 ... 保留 AppData 以备重装").
#
# Usage:
#     pwsh -ExecutionPolicy Bypass -File deploy\edge\windows\uninstall.ps1
#
# Optional:
#     -PurgeAppData   — also delete %APPDATA%\isales (token + sqlite + logs).
#                       D3 will fold this into a proper MSI uninstaller.

#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$PurgeAppData
)

$ErrorActionPreference = "Stop"

$AppName = "ISales"
$ProgramsDir = Join-Path $env:LOCALAPPDATA "Programs\isales"
$AppDataDir = Join-Path $env:APPDATA "isales"
$RunKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"

Write-Host "iSales edge client uninstaller" -ForegroundColor Cyan

# 1. Stop running process.
Get-Process -Name "isales-telephony" -ErrorAction SilentlyContinue | ForEach-Object {
    Write-Host "Stopping running process pid=$($_.Id) ..."
    Stop-Process -Id $_.Id -Force
    Start-Sleep -Milliseconds 500
}

# 2. Remove HKCU Run entry.
if (Test-Path $RunKey) {
    $existing = Get-ItemProperty -Path $RunKey -Name $AppName -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Removing HKCU\Run\$AppName ..." -ForegroundColor Cyan
        Remove-ItemProperty -Path $RunKey -Name $AppName
    }
}

# 3. Remove the Programs directory.
if (Test-Path $ProgramsDir) {
    Write-Host "Removing $ProgramsDir ..." -ForegroundColor Cyan
    Remove-Item -Recurse -Force -Path $ProgramsDir
}

# 4. Optionally purge AppData (token + sqlite + logs).
if ($PurgeAppData) {
    if (Test-Path $AppDataDir) {
        Write-Host "Removing $AppDataDir (-PurgeAppData specified) ..." -ForegroundColor Yellow
        Remove-Item -Recurse -Force -Path $AppDataDir
    }
} else {
    if (Test-Path $AppDataDir) {
        Write-Host "Preserving $AppDataDir (token + sqlite + logs) for future reinstall." `
            -ForegroundColor Cyan
        Write-Host "Pass -PurgeAppData to wipe these too."
    }
}

Write-Host ""
Write-Host "Uninstall complete." -ForegroundColor Green
