# =============================================================================
#  relay-supervisor.ps1  --  keep the OKX relay alive (restart when it dies)
#
#  WHY THE PATHS ARE RESOLVED FROM $PSScriptRoot AND NOT WRITTEN OUT:
#    PowerShell 5.1 reads a BOM-less file as ANSI (GBK on this box). Non-ASCII
#    characters inside the script text therefore get decoded wrongly, and a
#    literal path such as  D:\<cjk>\okx-terminal  silently becomes a MOJIBAKE
#    directory (the UTF-8 bytes reinterpreted as GBK). That is not a theory:
#    an earlier version of this script really did create and write into
#    D:\<mojibake>\okx-terminal\relay\logs\supervisor.log while the launcher
#    reported success.
#    $PSScriptRoot is resolved by the engine from the file system, so the real
#    path never passes through script-text decoding. This file is ASCII-only
#    for the same reason.
#
#  Start it DETACHED (so it survives the launching console):
#      wscript.exe <this dir>\start-supervisor.vbs
#  Stop it:
#      Stop-Process -Id (Get-Content <this dir>\logs\supervisor.pid)
#  Watch it:
#      Get-Content <this dir>\logs\supervisor.log -Tail 20 -Encoding UTF8
# =============================================================================

param(
    [int]$Port        = 8910,
    [int]$IntervalSec = 30
)

$ErrorActionPreference = 'Continue'

# --- resolve everything from the script's own location (no CJK literals) ---
$RelayDir = $PSScriptRoot
if ([string]::IsNullOrWhiteSpace($RelayDir)) {
    # -File invocation always sets it; guard for the odd inline case
    $RelayDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}
$relayPy = Join-Path $RelayDir 'relay.py'
$logDir  = Join-Path $RelayDir 'logs'
$supLog  = Join-Path $logDir  'supervisor.log'
$pidFile = Join-Path $logDir  'supervisor.pid'

$py = $env:OKX_PYTHON
if ([string]::IsNullOrWhiteSpace($py)) { $py = 'C:\Users\33096\.conda\envs\aiquant\python.exe' }

if (-not (Test-Path -LiteralPath $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }

function Write-SupLog([string]$msg) {
    $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try { [System.IO.File]::AppendAllText($supLog, $line + "`r`n", [System.Text.Encoding]::UTF8) } catch { }
}

function Test-RelayHealth {
    try {
        $r = Invoke-WebRequest -Uri ('http://127.0.0.1:{0}/health' -f $Port) -TimeoutSec 6 -UseBasicParsing
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Stop-PortOwner {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($conn) {
        try {
            Stop-Process -Id $conn.OwningProcess -Force -ErrorAction SilentlyContinue
            Write-SupLog ('killed stale listener pid=' + $conn.OwningProcess)
        } catch {
            Write-SupLog ('failed to kill pid=' + $conn.OwningProcess)
        }
    }
}

function Start-RelayDetached {
    # Start-Process is unusable here: the environment has both no_proxy and
    # NO_PROXY (and duplicated HTTP_PROXY/HTTPS_PROXY), and PowerShell builds a
    # case-insensitive dictionary from them, so every call throws
    #   ArgumentException: key 'no_proxy' already added 'NO_PROXY'
    # Building the ProcessStartInfo by hand avoids that constructor path.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName               = $py
    $psi.Arguments              = '"{0}" --port {1}' -f $relayPy, $Port
    $psi.WorkingDirectory       = $RelayDir
    $psi.UseShellExecute        = $false
    $psi.CreateNoWindow         = $true
    $psi.RedirectStandardOutput = $false
    $psi.RedirectStandardError  = $false
    $psi.EnvironmentVariables['PYTHONUTF8']       = '1'
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $proc = [System.Diagnostics.Process]::Start($psi)
    Write-SupLog ('relaunched relay pid=' + $proc.Id)
    return $proc
}

# --- single-instance guard -------------------------------------------------
if (Test-Path -LiteralPath $pidFile) {
    $old = ''
    try { $old = (Get-Content -LiteralPath $pidFile -Raw).Trim() } catch { }
    if ($old -match '^\d+$') {
        $alive = $null -ne (Get-Process -Id ([int]$old) -ErrorAction SilentlyContinue)
        if ($alive) {
            Write-Host ('[supervisor] already running pid=' + $old + ' -- exiting')
            exit 0
        }
    }
}
try { [System.IO.File]::WriteAllText($pidFile, "$PID", [System.Text.Encoding]::ASCII) } catch { }

Write-SupLog ('supervisor started pid={0} port={1} interval={2}s relayDir={3}' -f $PID, $Port, $IntervalSec, $RelayDir)
Write-Host ('[supervisor] pid=' + $PID + ' watching http://127.0.0.1:' + $Port + '/health every ' + $IntervalSec + 's')
Write-Host ('[supervisor] log=' + $supLog)

$failStreak = 0
while ($true) {
    Start-Sleep -Seconds $IntervalSec
    if (Test-RelayHealth) {
        if ($failStreak -gt 0) { Write-SupLog 'relay healthy again' }
        $failStreak = 0
        continue
    }
    $failStreak++
    Write-SupLog ('health check FAILED (streak=' + $failStreak + ')')
    if ($failStreak -ge 2) {
        Stop-PortOwner
        Start-Sleep -Seconds 2
        try { Start-RelayDetached | Out-Null } catch { Write-SupLog ('relaunch threw: ' + $_.Exception.Message) }
        $failStreak = 0
        Start-Sleep -Seconds 25
    }
}
