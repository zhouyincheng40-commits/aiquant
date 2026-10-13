# POLY Terminal static server (portable)
# Usage:
#   pwsh -File .\start-server.ps1
#   pwsh -File .\start-server.ps1 -Port 8899
# Env:
#   AIQUANT_HOME  workspace root (default: this script's directory)
#   POLY_HTTP_PORT  override port
#
# Notes:
#   - Serves the whole workspace tree so /data/poly/edges.json resolves.
#   - For Polymarket live panels, a reachable proxy may be required
#     (set POLY_PROXY / system proxy). On cloud without Clash, panels that
#     call polymarket domains will show OFFLINE unless egress is allowed.

param(
  [int]$Port = 0
)

$ErrorActionPreference = 'Stop'
$Root = if ($env:AIQUANT_HOME -and $env:AIQUANT_HOME.Trim()) {
  $env:AIQUANT_HOME.Trim()
} else {
  $PSScriptRoot
}
if (-not (Test-Path -LiteralPath $Root)) {
  throw "AIQUANT_HOME / script root not found: $Root"
}

if ($Port -le 0) {
  if ($env:POLY_HTTP_PORT -and [int]::TryParse($env:POLY_HTTP_PORT, [ref]$Port)) { }
  else { $Port = 8899 }
}

$pyCandidates = @(
  $env:AIQUANT_PYTHON,
  (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
  (Get-Command python3 -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source),
  'C:\Users\33096\.conda\envs\aiquant\python.exe'
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }

if (-not $pyCandidates) {
  throw "No Python found. Set AIQUANT_PYTHON or install python3."
}
$Py = $pyCandidates[0]

$page = "/web/poly-terminal/index.html"
Write-Host "[POLY] root=$Root"
Write-Host "[POLY] python=$Py"
Write-Host "[POLY] http://127.0.0.1:$Port$page"
Write-Host "[POLY] Ctrl+C to stop. If Polymarket panels are OFFLINE, check proxy/egress."

Set-Location -LiteralPath $Root
& $Py -m http.server $Port --bind 127.0.0.1 --directory $Root
