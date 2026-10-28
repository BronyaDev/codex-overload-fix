#Requires -Version 5.1
[CmdletBinding()]
param([Parameter(Mandatory = $true)][ValidateSet('Install', 'RemoveConfig', 'Uninstall')][string]$Action)

$ErrorActionPreference = 'Stop'
try {
    $python = $null
    foreach ($candidate in @('py', 'python', 'python3')) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        $pythonArgs = @()
        if ($candidate -eq 'py') { $pythonArgs = @('-3') }
        try {
            $found = & $candidate @pythonArgs -c 'import sys; assert sys.version_info >= (3,9); print(sys.executable)' 2>$null
            if ($LASTEXITCODE -eq 0 -and $found -and (Test-Path -LiteralPath ($found | Select-Object -Last 1))) {
                $python = ($found | Select-Object -Last 1).Trim()
                break
            }
        } catch { continue }
    }
    if (-not $python) { throw 'Python 3.9+ was not found. Install Python, then run this script again.' }
    $helper = Join-Path $PSScriptRoot 'configure_codex.py'
    if ($Action -eq 'Install') {
        & $python $helper check
        if ($LASTEXITCODE -ne 0) { throw 'Configuration preflight failed; installer was not started.' }
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'install.ps1')
        if ($LASTEXITCODE -ne 0) { throw 'Default installer failed; Codex configuration was not changed.' }
        & $python $helper install
        if ($LASTEXITCODE -ne 0) { throw 'Relay installed, but configuration failed. Review the error above; rerun install.cmd to retry.' }
    } else {
        & $python $helper remove
        if ($LASTEXITCODE -ne 0) { throw 'Configuration removal failed; relay uninstall was not started.' }
        if ($Action -eq 'Uninstall') {
            & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'uninstall.ps1')
            if ($LASTEXITCODE -ne 0) { throw 'Configuration removed, but the default uninstaller failed. Rerun uninstall.cmd.' }
        }
    }
    Write-Host ''
    Write-Host 'Done. Fully exit and reopen Codex for the configuration to take effect.' -ForegroundColor Green
    Write-Host 'Keep this repository folder in place while the relay is installed.' -ForegroundColor Yellow
    exit 0
} catch {
    Write-Host ('FAILED: ' + $_.Exception.Message) -ForegroundColor Red
    exit 1
}
