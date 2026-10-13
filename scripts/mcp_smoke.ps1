# -*- coding: utf-8 -*-
<#
    mcp_smoke.ps1 — quant_mcp (serverName: aiquant) stdio JSON-RPC 端到端冒烟测试

    用法:  powershell -ExecutionPolicy Bypass -File D:\量化\scripts\mcp_smoke.ps1

    流程: 启动 server.py (stdio) -> initialize -> notifications/initialized
          -> tools/list (断言 12 个工具) -> 真实 tools/call q_quote(600519)
          -> 真实 tools/call q_klines(600519) -> 打印关键返回

    退出码: 0 = 全部假设通过, 1 = 有断言失败
    要点: 服务器 stdout 只输出 JSON-RPC；日志走 stderr（本脚本分开读，绝不混读）。
#>
[CmdletBinding()]
param(
    [string]$Python = 'C:\Users\33096\.conda\envs\aiquant\python.exe',
    [string]$Server = 'D:\量化\mcp\quant_mcp\server.py',
    [int]$TimeoutSec = 180
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$script:failures = New-Object System.Collections.Generic.List[string]
function Assert-True {
    param([bool]$Condition, [string]$Label, [string]$Extra = '')
    if ($Condition) { Write-Host ("  [PASS] " + $Label) -ForegroundColor Green }
    else {
        Write-Host ("  [FAIL] " + $Label + " " + $Extra) -ForegroundColor Red
        $script:failures.Add($Label)
    }
}

Write-Host '===== quant_mcp MCP 冒烟测试 =====' -ForegroundColor Cyan
Write-Host ("Python : " + $Python)
Write-Host ("Server : " + $Server)
Write-Host ''

Assert-True (Test-Path -LiteralPath $Python) 'python 解释器存在'
Assert-True (Test-Path -LiteralPath $Server) 'server.py 存在'
if ($script:failures.Count -gt 0) { exit 1 }

# ---- 启动 stdio 服务 ----
$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName               = $Python
$psi.Arguments              = '-u "' + $Server + '"'
$psi.UseShellExecute        = $false
$psi.RedirectStandardInput  = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError  = $true
$psi.CreateNoWindow         = $true
$psi.StandardOutputEncoding = [System.Text.Encoding]::UTF8
$psi.StandardErrorEncoding  = [System.Text.Encoding]::UTF8

$proc = New-Object System.Diagnostics.Process
$proc.StartInfo = $psi
[void]$proc.Start()
Write-Host ("启动 stdio 服务 pid=" + $proc.Id) -ForegroundColor DarkGray
Start-Sleep -Milliseconds 800
Assert-True (-not $proc.HasExited) '服务进程存活（未启动即退出）'

# 异步排空 stderr，避免管道写满阻塞服务器
$errLines = New-Object System.Collections.Generic.List[string]
$errHandler = { if ($null -ne $EventArgs.Data) { $errLines.Add($EventArgs.Data) } }
$null = Register-ObjectEvent -InputObject $proc -EventName ErrorDataReceived -Action $errHandler
$proc.BeginErrorReadLine()

$writer = $proc.StandardInput
$reader = $proc.StandardOutput

function Send-Rpc {
    param([string]$Line)
    $writer.Write($Line + "`n")
    $writer.Flush()
}

# 只读 stdout（纯 JSON-RPC），逐行返回第一个 JSON 对象
function Read-Rpc {
    param([string]$Label, [int]$Timeout = 0)
    if ($Timeout -le 0) { $Timeout = $TimeoutSec }
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    while ($sw.Elapsed.TotalSeconds -lt $Timeout) {
        $task = $reader.ReadLineAsync()
        if (-not $task.Wait([TimeSpan]::FromSeconds($Timeout))) {
            throw ("读取 " + $Label + " 超时（>" + $Timeout + "s）")
        }
        $line = $task.Result
        if ($null -eq $line) { throw ("读取 " + $Label + " 时 stdout 已关闭") }
        $t = $line.Trim()
        if ($t.Length -eq 0) { continue }
        if (-not $t.StartsWith('{')) { continue }   # 非 JSON 一律忽略
        return ($t | ConvertFrom-Json)
    }
    throw ("读取 " + $Label + " 超时（>" + $Timeout + "s）")
}

try {
    # ---- 1. initialize ----
    Write-Host '[1/4] initialize ...'
    Send-Rpc '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"mcp_smoke.ps1","version":"1.0.0"}}}'
    $r1 = Read-Rpc 'initialize' 30
    Assert-True ($r1.jsonrpc -eq '2.0') '响应 jsonrpc 为 2.0'
    Assert-True ($r1.id -eq 1) '响应 id 回显正确'
    Assert-True ($r1.result.protocolVersion -eq '2024-11-05') 'protocolVersion = 2024-11-05' ("实际: " + $r1.result.protocolVersion)
    Assert-True ($r1.result.serverInfo.name -eq 'aiquant') 'serverInfo.name = aiquant' ("实际: " + $r1.result.serverInfo.name)
    Write-Host ("       serverInfo: " + $r1.result.serverInfo.name + " v" + $r1.result.serverInfo.version) -ForegroundColor DarkGray

    # ---- 2. notifications/initialized (通知，无响应) ----
    Write-Host '[2/4] notifications/initialized ...'
    Send-Rpc '{"jsonrpc":"2.0","method":"notifications/initialized"}'
    Start-Sleep -Milliseconds 200

    # ---- 3. tools/list ----
    Write-Host '[3/4] tools/list ...'
    Send-Rpc '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
    $r2 = Read-Rpc 'tools/list' 30
    $toolNames = @($r2.result.tools | ForEach-Object { $_.name })
    Assert-True ($toolNames.Count -eq 12) '工具数量 = 12' ("实际: " + $toolNames.Count)
    foreach ($t in @('q_env', 'q_quote', 'q_klines', 'q_indicators', 'q_backtest_ma', 'q_optimize_grid',
                     'q_factor_ic', 'q_market_overview', 'q_screen', 'q_crypto_klines',
                     'q_export_csv', 'q_cache_clear')) {
        Assert-True ($toolNames -contains $t) ("包含工具 " + $t)
    }
    Write-Host ("       工具: " + ($toolNames -join ', ')) -ForegroundColor DarkGray

    # ---- 4. 真实 tools/call q_quote(600519) ----
    Write-Host '[4/4] tools/call q_quote 600519 ...'
    Send-Rpc '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"q_quote","arguments":{"symbols":["600519"]}}}'
    $r3 = Read-Rpc 'q_quote' 90
    Assert-True ($null -ne $r3.result) 'q_quote 返回 result'
    Assert-True ($r3.result.isError -eq $false) 'q_quote isError = false' ("实际: " + $r3.result.isError)
    $quoteText = [string]$r3.result.content[0].text
    Assert-True ($quoteText -match '600519') 'q_quote 含代码 600519'
    Assert-True ($quoteText -match '贵州茅台') 'q_quote 含名称 贵州茅台'
    Assert-True ($quoteText -match '\d+\.\d+\s*%') 'q_quote 含真实涨跌幅百分比'

    # ---- 5. 真实 tools/call q_klines(600519) ----
    Write-Host '[5/5] tools/call q_klines 600519 ...'
    Send-Rpc '{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"q_klines","arguments":{"symbol":"600519","period":"daily","start":"20260101","end":"20261231","limit":250}}}'
    $r4 = Read-Rpc 'q_klines' 120
    Assert-True ($null -ne $r4.result) 'q_klines 返回 result'
    Assert-True ($r4.result.isError -eq $false) 'q_klines isError = false' ("实际: " + $r4.result.isError)
    $kText = [string]$r4.result.content[0].text
    Assert-True ($kText -match '20\d\d-\d\d-\d\d') 'q_klines 含真实日期 YYYY-MM-DD'
    Assert-True ($kText -match 'K线根数') 'q_klines 含区间统计'
} catch {
    Write-Host ("  [FAIL] " + $_.Exception.Message) -ForegroundColor Red
    $script:failures.Add($_.Exception.Message)
} finally {
    try { $writer.Close() } catch { }
    try { if (-not $proc.WaitForExit(15000)) { $proc.Kill() } } catch { }
}

# ---- 打印关键返回 ----
Write-Host ''
Write-Host '===== 关键返回 =====' -ForegroundColor Cyan
if ($r1) {
    Write-Host ('initialize -> ' + ($r1.result | ConvertTo-Json -Depth 6 -Compress)) -ForegroundColor DarkGray
}
if ($r2) {
    Write-Host ('tools/list -> ' + $toolNames.Count + ' tools') -ForegroundColor DarkGray
}
if ($quoteText) {
    Write-Host ''
    Write-Host '----- q_quote(600519) 原文 -----' -ForegroundColor Cyan
    Write-Host $quoteText
}
if ($kText) {
    Write-Host ''
    Write-Host '----- q_klines(600519) 原文 -----' -ForegroundColor Cyan
    Write-Host $kText
}
if ($errLines.Count -gt 0) {
    Write-Host ''
    Write-Host '----- 服务器 stderr (前 8 行，仅作诊断) -----' -ForegroundColor DarkYellow
    $errLines | Select-Object -First 8 | ForEach-Object { Write-Host ('  ' + $_) }
}

# ---- 汇总 ----
Write-Host ''
Write-Host '===== 汇总 =====' -ForegroundColor Cyan
if ($script:failures.Count -eq 0) {
    Write-Host '全部断言通过 ✅' -ForegroundColor Green
    exit 0
} else {
    Write-Host ("失败 " + $script:failures.Count + " 项:") -ForegroundColor Red
    $script:failures | ForEach-Object { Write-Host ('  - ' + $_) } -ForegroundColor Red
    exit 1
}
