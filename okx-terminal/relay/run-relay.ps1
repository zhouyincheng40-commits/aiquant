# run-relay.ps1 -- launcher for the OKX realtime relay backend.
# Pure ASCII on purpose: the console code page here is 936 (GBK).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File D:\<path>\run-relay.ps1
#   powershell -ExecutionPolicy Bypass -File .\run-relay.ps1 --port 8910
#   powershell -ExecutionPolicy Bypass -File .\run-relay.ps1 --no-prime -v
#
# Any extra arguments are forwarded to relay.py unchanged.

$ErrorActionPreference = "Stop"

$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$Py = "C:\Users\33096\.conda\envs\aiquant\python.exe"
$Script = Join-Path $PSScriptRoot "relay.py"

if (-not (Test-Path $Py)) { Write-Error "python not found: $Py"; exit 1 }
if (-not (Test-Path $Script)) { Write-Error "relay.py not found: $Script"; exit 1 }

Write-Host "[run-relay] python : $Py"
Write-Host "[run-relay] script : $Script"
Write-Host "[run-relay] url    : http://127.0.0.1:8910/  (ws /ws, sse /sse, debug /debug)"
Write-Host "[run-relay] stop   : Ctrl+C"

& $Py $Script @args
exit $LASTEXITCODE
