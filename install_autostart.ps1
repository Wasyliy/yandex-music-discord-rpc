# Creates (or removes) a shortcut in the Windows Startup folder that runs
# yandex_rpc.py silently via pythonw.exe at logon.
#
# Install:  powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1
# Remove:   powershell -ExecutionPolicy Bypass -File .\install_autostart.ps1 -Remove

param([switch]$Remove)

$ErrorActionPreference = "Stop"

$root    = Split-Path -Parent $MyInvocation.MyCommand.Path
$script  = Join-Path $root "yandex_rpc.py"
$lnkPath = Join-Path ([Environment]::GetFolderPath("Startup")) "YandexMusicRPC.lnk"

if ($Remove) {
    if (Test-Path $lnkPath) {
        Remove-Item $lnkPath
        Write-Host "Autostart shortcut removed: $lnkPath"
    } else {
        Write-Host "Nothing to remove."
    }
    exit 0
}

if (-not (Test-Path $script)) { throw "yandex_rpc.py not found next to this script." }
if (-not (Test-Path (Join-Path $root "config.json"))) {
    Write-Warning "config.json not found - copy config.example.json to config.json first."
}

# Find pythonw.exe (windowless Python)
$pythonw = (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source
if (-not $pythonw) {
    $py = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $py) { throw "Python not found in PATH." }
    $pythonw = Join-Path (Split-Path $py) "pythonw.exe"
}
if (-not (Test-Path $pythonw)) { throw "pythonw.exe not found: $pythonw" }

$ws  = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($lnkPath)
$lnk.TargetPath       = $pythonw
$lnk.Arguments        = "`"$script`""
$lnk.WorkingDirectory = $root
$lnk.Save()

Write-Host "Autostart shortcut created: $lnkPath"
Write-Host "Target: $pythonw `"$script`""
