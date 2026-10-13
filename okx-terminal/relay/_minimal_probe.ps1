param([string]$RelayDir = 'D:\量化\okx-terminal\relay')

$p = $PID
$lp = Join-Path $RelayDir 'logs'

# deliberately do the same three things the supervisor does, in the same order,
# and trap everything so we can see which one fails
try {
    if (-not (Test-Path $lp)) { New-Item -ItemType Directory -Path $lp -Force | Out-Null }
    [System.IO.File]::WriteAllText((Join-Path $lp 'minimal_probe_marker.txt'),
        "pid=$p started=$(Get-Date -Format o)`n", [System.Text.Encoding]::UTF8)
} catch {
    [System.IO.File]::WriteAllText('D:\minimal_probe_error.txt', $_.Exception.ToString(), [System.Text.Encoding]::UTF8)
}

Start-Sleep -Seconds 120
