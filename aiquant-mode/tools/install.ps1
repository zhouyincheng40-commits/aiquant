# dsh-aiquant-mode 部署/同步脚本（开发与升级都走它）
#
# 为什么需要：profile 用 pnpm 从本目录安装了一份**副本**到
#   $DSH_HOME\profiles\desktop\node_modules\dsh-aiquant-mode
# 改完源码不会自动生效，必须同步副本 + 重启/刷新 DSH。
#
# 用法：
#   pwsh -File tools\install.ps1              # 生成 cordis.patch.yml + 同步到 desktop profile
#   pwsh -File tools\install.ps1 -Profile web # 指定其它 profile
#   pwsh -File tools\install.ps1 -NoBuild     # 只同步，不重新生成补丁

[CmdletBinding()]
param(
  [string]$Profile = 'desktop',
  [switch]$NoBuild
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dshHome = if ($env:DSH_HOME) { $env:DSH_HOME } else { Join-Path $env:USERPROFILE '.dsh' }
$target = Join-Path $dshHome ("profiles\{0}\node_modules\dsh-aiquant-mode" -f $Profile)
$node = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $node) { $node = 'C:\Program Files\nodejs\node.exe' }

if (-not $NoBuild) {
  Write-Host '[1/3] 生成 cordis.patch.yml ...' -ForegroundColor Cyan
  & $node (Join-Path $root 'tools\build.mjs')
  if ($LASTEXITCODE -ne 0) { throw 'cordis.patch.yml 生成失败' }
} else {
  Write-Host '[1/3] 跳过补丁生成（-NoBuild）' -ForegroundColor DarkGray
}

if (-not (Test-Path $target)) {
  Write-Host ("[2/3] 目标不存在：{0}" -f $target) -ForegroundColor Yellow
  Write-Host '      先安装 bundle：在 DSH 里对 dsh-aiquant-mode 执行安装，或跑：' -ForegroundColor Yellow
  Write-Host ("      dsh plugin --profile {0} add file:{1}" -f $Profile, ($root -replace '\\', '/')) -ForegroundColor Yellow
  throw 'profile 里还没有 dsh-aiquant-mode，无法同步'
}

Write-Host ("[2/3] 同步到 {0}" -f $target) -ForegroundColor Cyan
foreach ($item in @('lib', 'presets', 'scripts', 'tools', 'skills')) {
  $src = Join-Path $root $item
  if (-not (Test-Path $src)) { continue }
  Copy-Item -Path $src -Destination $target -Recurse -Force
}
foreach ($file in @('package.json', 'cordis.patch.yml', 'README.md')) {
  $src = Join-Path $root $file
  if (Test-Path $src) { Copy-Item -Path $src -Destination $target -Force }
}
$count = (Get-ChildItem (Join-Path $target 'lib') -File).Count
Write-Host ("      已同步 {0} 个 lib 文件" -f $count) -ForegroundColor Green

Write-Host '[3/3] 生效方式' -ForegroundColor Cyan
Write-Host '      · 新增/变更客户端 bundle 或 profile bundles：完整重启 DSH（不是刷新）'
Write-Host '      · 仅改 host 侧 JS：重启 DSH 后即生效（host HMR 只负责 stat 检测，不重载新包）'
Write-Host '      · 客户端看板改动：重启后按 F5 刷新页面'
