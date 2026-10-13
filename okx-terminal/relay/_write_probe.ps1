$results = @()
$targets = @(
    @{ name = 'temp';        path = (Join-Path $env:TEMP 'probe_temp.txt') },
    @{ name = 'c_root';      path = 'C:\probe_croot.txt' },
    @{ name = 'd_root';      path = 'D:\probe_droot.txt' },
    @{ name = 'okx_logs';    path = 'D:\量化\okx-terminal\relay\logs\probe_logs.txt' },
    @{ name = 'quant_root';  path = 'D:\量化\probe_quant.txt' }
)

foreach ($t in $targets) {
    $ok = $false
    $err = ''
    try {
        [System.IO.File]::WriteAllText($t.path, "pid=$PID exe=$([System.Diagnostics.Process]::GetCurrentProcess().MainModule.FileName)`n", [System.Text.Encoding]::UTF8)
        $ok = [System.IO.File]::Exists($t.path)
    } catch {
        $err = $_.Exception.GetType().Name + ': ' + $_.Exception.Message
    }
    $results += ('{0,-12} ok={1,-6} path={2} {3}' -f $t.name, $ok, $t.path, $err)
}

$results += ('current dir = ' + (Get-Location).Path)
$results += ('user = ' + [System.Security.Principal.WindowsIdentity]::GetCurrent().Name)

[System.IO.File]::WriteAllText('C:\probe_results.txt', ($results -join "`n"), [System.Text.Encoding]::UTF8)
$results | ForEach-Object { Write-Host $_ }
