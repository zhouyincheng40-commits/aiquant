$report = 'D:\量化\okx-terminal\relay\logs\_diag_report.txt'
$lines = New-Object System.Collections.ArrayList
function Add-Line($s) { [void]$lines.Add([string]$s) }

Add-Line "=== diagnosis ==="
Add-Line ("pid        = " + $PID)
Add-Line ("ps version = " + $PSVersionTable.PSVersion)
Add-Line ("exe        = " + [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName)
Add-Line ("cwd        = " + (Get-Location).Path)
Add-Line ""

# param binding as seen by the script
Add-Line "=== bound params ==="
Add-Line ("Port=$Port")
Add-Line ("IntervalSec=$IntervalSec")
Add-Line ("RelayDir=[$RelayDir]")
Add-Line ("RelayDir len=" + $RelayDir.Length)
Add-Line ("RelayDir utf8 bytes = " + (([System.Text.Encoding]::UTF8.GetBytes($RelayDir) | ForEach-Object { $_.ToString('X2') }) -join ' '))
Add-Line ""

Add-Line "=== path resolution ==="
$logDir = Join-Path $RelayDir 'logs'
Add-Line ("logDir     = [$logDir]")
Add-Line ("logDir exists = " + (Test-Path -LiteralPath $logDir))
Add-Line ("test RelayDir exists = " + (Test-Path -LiteralPath $RelayDir))
Add-Line ""

Add-Line "=== write attempts ==="
foreach ($p in @(
    'D:\量化\okx-terminal\relay\logs\_w1.txt',
    'D:\量化\_w2.txt'
)) {
    try {
        [System.IO.File]::WriteAllText($p, "pid=$PID`n", [System.Text.Encoding]::UTF8)
        Add-Line ("write OK   : " + $p)
    } catch {
        Add-Line ("write FAIL : " + $p + " :: " + $_.Exception.GetType().Name + " :: " + $_.Exception.Message)
    }
}
Add-Line ""
Add-Line "DONE"

[System.IO.File]::WriteAllText($report, ($lines -join "`r`n"), [System.Text.Encoding]::UTF8)
