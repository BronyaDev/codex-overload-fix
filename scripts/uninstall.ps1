#Requires -Version 5.1
<#
.SYNOPSIS
    Stop the Codex overload relay and remove its autostart entry.

.DESCRIPTION
    Removes the HKCU Run entry, terminates the watchdog and relay processes,
    and optionally deletes the generated launchers and logs.

    It does NOT touch your Codex config.toml - you have to remove the
    model_provider line yourself so you can see exactly what changed.

.PARAMETER Purge
    Also delete %LOCALAPPDATA%\codex-overload-fix (launchers and logs).
#>
[CmdletBinding()]
param([switch]$Purge)

$ErrorActionPreference = 'Stop'

function Write-Step($msg) { Write-Host "  $msg" -ForegroundColor DarkGray }
function Write-Ok($msg)   { Write-Host "  [ok] $msg" -ForegroundColor Green }

Write-Host ""
Write-Host "Codex Overload Fix - uninstaller" -ForegroundColor Cyan
Write-Host "-------------------------------" -ForegroundColor DarkGray

$stateDir = Join-Path $env:LOCALAPPDATA 'codex-overload-fix'
$runKey   = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$runName  = 'CodexOverloadRelay'

# ---------------------------------------------------------------- autostart
try {
    $existing = Get-ItemProperty -Path $runKey -Name $runName -ErrorAction Stop
    if ($existing) {
        Remove-ItemProperty -Path $runKey -Name $runName
        Write-Ok "removed $runName from HKCU Run"
    }
} catch {
    Write-Step 'no autostart entry found'
}

# ---------------------------------------------------------------- processes
$killed = 0
Get-CimInstance Win32_Process -Filter "Name = 'wscript.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*watch-relay.vbs*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }

Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like '*relay.py*' } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $killed++
    }

if ($killed -gt 0) {
    Write-Ok "stopped $killed process(es)"
} else {
    Write-Step 'nothing was running'
}

# ---------------------------------------------------------------- purge
if ($Purge) {
    if (Test-Path $stateDir) {
        Remove-Item -Recurse -Force $stateDir
        Write-Ok "deleted $stateDir"
    }
}

Write-Host ""
Write-Host "Now remove these lines from $env:USERPROFILE\.codex\config.toml:" -ForegroundColor Yellow
Write-Host "----------------------------------------" -ForegroundColor DarkGray
Write-Host 'model_provider = "openai-retry"' -ForegroundColor White
Write-Host '' -ForegroundColor White
Write-Host '[model_providers.openai-retry]' -ForegroundColor White
Write-Host 'name = "OpenAI (retry)"' -ForegroundColor White
Write-Host 'base_url = "http://127.0.0.1:8787"' -ForegroundColor White
Write-Host 'wire_api = "responses"' -ForegroundColor White
Write-Host 'requires_openai_auth = true' -ForegroundColor White
Write-Host 'stream_max_retries = 20' -ForegroundColor White
Write-Host 'request_max_retries = 10' -ForegroundColor White
Write-Host "----------------------------------------" -ForegroundColor DarkGray
Write-Host ""
