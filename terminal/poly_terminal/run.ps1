# =============================================================================
#  POLY // TERMINAL - PowerShell launcher
#
#  Why this file exists:
#    PowerShell does not load commands from the current directory by default
#    (Command Precedence). Typing  run.cmd  therefore fails with
#    "CommandNotFoundException"; you must type  .\run.cmd
#    This launcher lets you just run  .\run.ps1  and it also pins UTF-8,
#    which is mandatory on this box (chcp = 936/GBK would otherwise kill
#    the cent sign with UnicodeEncodeError).
#
#  Usage:
#    .\run.ps1                     live full-screen mode
#    .\run.ps1 --once              print one screen and exit
#    .\run.ps1 --once --color      one screen with ANSI colour
#    .\run.ps1 --rows 8 --width 150
#
#  Requirements:
#    - Proxy 127.0.0.1:7890 must be running (Clash / mihomo) for the three
#      Polymarket domains. The CN market band works direct either way.
# =============================================================================

$ErrorActionPreference = 'Stop'

$py = 'C:\Users\33096\.conda\envs\aiquant\python.exe'
if (-not (Test-Path $py)) {
    Write-Host "[POLY] python interpreter not found: $py" -ForegroundColor Red
    exit 1
}

# UTF-8 is mandatory on this machine (see header note).
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$app = Join-Path $PSScriptRoot 'app.py'
& $py $app @args
exit $LASTEXITCODE
