# AI量化模式环境安装/自检脚本（随包分发，落到 $DSH_HOME/aiquant/setup.ps1）
#
# 用法（推荐在 DSH 会话里让「AI量化模式」的智能体替你跑）：
#   pwsh -File C:\Users\33096\.dsh\aiquant\setup.ps1 -Check          # 只自检，不改任何东西
#   pwsh -File C:\Users\33096\.dsh\aiquant\setup.ps1 -Fix            # 建目录/装脚本，缺什么补什么
#
# 它做四件事：
#   1. 检查 conda 环境（aiquant 主环境 / aiquant-live 实盘环境）、关键 Python 包；
#   2. 建工作区目录骨架（data/cache、data/exports、strategies、reports、logs、notebooks、skills）；
#   3. 检查 MCP 服务器文件与 profile 里的 MCP 行；检查系统代理与数据源可达性；
#   4. 打印事实库与看板位置，方便用户直接打开。
#
# 注意：本脚本**不写任何账号密钥**；CTP/SimNow 凭据一律从环境变量读。

[CmdletBinding()]
param(
  [switch]$Check,
  [switch]$Fix
)

$ErrorActionPreference = 'Continue'
$workspace = if ($env:AIQUANT_HOME) { $env:AIQUANT_HOME } else { 'D:\量化' }
$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { join-path $env:USERPROFILE '.dsh' }
$python = if ($env:AIQUANT_PYTHON) { $env:AIQUANT_PYTHON } else { 'C:\Users\33096\.conda\envs\aiquant\python.exe' }
$pythonLive = 'C:\Users\33096\.conda\envs\aiquant-live\python.exe'

$script:failures = 0
function Say([string]$level, [string]$text) {
  $color = switch ($level) { 'OK' { 'Green' } 'WARN' { 'Yellow' } 'FAIL' { 'Red' } default { 'Gray' } }
  Write-Host ("[{0}] {1}" -f $level, $text) -ForegroundColor $color
  if ($level -eq 'FAIL') { $script:failures++ }
}

Write-Host "=== AI量化模式环境自检 ===" -ForegroundColor Cyan
Write-Host ("工作区 : {0}" -f $workspace)
Write-Host ("DSH_HOME: {0}" -f $dshHome)
Write-Host ""

# ---------- 1. 解释器与依赖 ----------
if (Test-Path $python) {
  $version = & $python -c "import sys;print(sys.version.split()[0])" 2>$null
  Say OK ("主环境 aiquant : {0} (Python {1})" -f $python, $version)
  $probe = & $python -c "import importlib.util as u;mods=['akshare','pandas','numpy','talib','backtrader','vectorbt','lightgbm','xgboost'];print(','.join(m for m in mods if u.find_spec(m)))" 2>$null
  if ($probe) { Say OK ("已装关键包 : {0}" -f $probe) } else { Say WARN "主环境未探测到关键包（akshare/pandas/numpy 至少要有）" }
} else {
  Say FAIL ("主环境解释器不存在 : {0}" -f $python)
}
if (Test-Path $pythonLive) { Say OK ("实盘环境 aiquant-live : {0}" -f $pythonLive) } else { Say WARN ("实盘环境不存在 : {0}（只影响实盘链路）" -f $pythonLive) }

# ---------- 2. 工作区目录 ----------
$dirs = @('data', 'data\cache', 'data\exports', 'strategies', 'reports', 'logs', 'notebooks', 'skills')
foreach ($rel in $dirs) {
  $full = Join-Path $workspace $rel
  if (Test-Path $full) { Say OK ("目录存在 : {0}" -f $full) }
  elseif ($Fix) { New-Item -ItemType Directory -Path $full -Force | Out-Null; Say OK ("已创建 : {0}" -f $full) }
  else { Say WARN ("目录缺失 : {0}（跑 -Fix 自动建）" -f $full) }
}

# ---------- 3. MCP 服务器与 profile 行 ----------
$mcpServer = Join-Path $workspace 'mcp\quant_mcp\server.py'
if (Test-Path $mcpServer) {
  $size = [math]::Round((Get-Item $mcpServer).Length / 1024, 1)
  Say OK ("MCP 服务器 : {0} ({1} KB)" -f $mcpServer, $size)
  $ast = & $python -c "import ast,sys;ast.parse(open(sys.argv[1],encoding='utf-8').read());print('AST OK')" $mcpServer 2>&1
  if ($ast -match 'AST OK') { Say OK "MCP 服务器语法检查通过" } else { Say FAIL ("MCP 服务器语法错误 : {0}" -f $ast) }
} else {
  Say FAIL ("MCP 服务器缺失 : {0}（q_* 工具不可用）" -f $mcpServer)
}
$patchFile = Join-Path $dshHome 'profiles\desktop\cordis.patch.yml'
if (Test-Path $patchFile) {
  $patchText = Get-Content -LiteralPath $patchFile -Raw -Encoding UTF8
  if ($patchText -match 'aiquant-mode-mcp') { Say OK "profile 补丁已声明 aiquant-mode-mcp（量化 MCP 行）" }
  else { Say WARN "profile 补丁里没有 aiquant-mode-mcp：确认 dsh-aiquant-mode 已装进 profile bundles 并重启 DSH" }
  if ($patchText -match 'D:\\AI量化') { Say WARN "profile 补丁里仍残留 D:\AI量化 的旧路径（该目录已不存在），建议删掉旧 mcp-quant 段" }
} else {
  Say WARN ("未找到 profile 补丁 : {0}" -f $patchFile)
}

# ---------- 4. 代理与数据源 ----------
$proxy = (Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue).ProxyServer
if ($proxy) { Say OK ("系统代理 : {0}" -f $proxy) } else { Say WARN "系统代理未配置（境外数据源需直连可达或设 AIQUANT_PROXY）" }
$probes = @(
  @{ Name = '腾讯行情(A股)'; Url = 'https://qt.gtimg.cn/q=sh600519' },
  @{ Name = '腾讯日线'; Url = 'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sh600519,day,,,20,qfq' },
  @{ Name = '新浪外汇(加密现货)'; Url = 'https://hq.sinajs.cn/list=fx_sbtcusd' },
  @{ Name = 'Binance(需代理)'; Url = 'https://api.binance.com/api/v3/ping' }
)
foreach ($probe in $probes) {
  try {
    $sw = [Diagnostics.Stopwatch]::StartNew()
    Invoke-WebRequest -Uri $probe.Url -TimeoutSec 8 -UseBasicParsing -Headers @{ Referer = 'https://finance.sina.com.cn' } | Out-Null
    Say OK ("数据源可用 : {0} ({1} ms)" -f $probe.Name, [int]$sw.ElapsedMilliseconds)
  } catch {
    Say WARN ("数据源不可达 : {0} —— {1}" -f $probe.Name, $_.Exception.Message.Split("`n")[0])
  }
}

# ---------- 5. 事实库与看板 ----------
$dbFile = Join-Path $dshHome 'aiquant\aiquant.db'
if (Test-Path $dbFile) { Say OK ("事实库 : {0} ({1} KB)" -f $dbFile, [math]::Round((Get-Item $dbFile).Length / 1024, 1)) }
else { Say WARN ("事实库尚未创建 : {0}（重启 DSH 后由 aiquant-mode-store 建立）" -f $dbFile) }
$skillsDir = Join-Path $workspace 'skills'
$skillCount = 0
if (Test-Path $skillsDir) { $skillCount = (Get-ChildItem $skillsDir -Directory -ErrorAction SilentlyContinue | Where-Object { Test-Path (Join-Path $_.FullName 'SKILL.md') }).Count }
Say OK ("量化技能 : {0} 个（{1}）" -f $skillCount, $skillsDir)
Say OK "看板位置 : DSH 右侧常驻「AI量化 看板」（侧栏底部「AI量化」按钮可开关）"

Write-Host ""
if ($script:failures -eq 0) {
  Write-Host "自检完成：无阻塞项。" -ForegroundColor Green
} else {
  Write-Host ("自检完成：{0} 项阻塞，见上面 [FAIL] 行。" -f $script:failures) -ForegroundColor Red
  exit 1
}
