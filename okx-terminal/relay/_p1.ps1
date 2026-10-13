$ErrorActionPreference = 'Continue'
$out = 'D:\量化\okx-terminal\relay\logs\_p1.txt'
$lines = @()
$lines += "pid=$PID"
$lines += "exe=" + [System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName
$lines += "cwd=" + (Get-Location).Path
try {
    [System.IO.File]::WriteAllText($out, ($lines -join "`r`n"), [System.Text.Encoding]::UTF8)
    $lines += "write=OK"
} catch {
    $lines += "write=FAIL " + $_.Exception.Message
}
# second attempt, plain Set-Content
try {
    Set-Content -LiteralPath 'D:\量化\okx-terminal\relay\logs\_p1b.txt' -Value 'hello' -Encoding ASCII
    $lines += "setcontent=OK"
} catch {
    $lines += "setcontent=FAIL " + $_.Exception.Message
}
# echo what we decided
$lines | ForEach-Object { Write-Host $_ }
