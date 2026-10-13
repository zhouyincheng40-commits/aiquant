# Capture a window (by process name) to a PNG using PrintWindow (works for occluded windows).
# Usage: powershell -NoProfile -File capture-window.ps1 -ProcessName "DeepSeek Harness" -Out "D:\path\shot.png"
param(
  [string]$ProcessName = 'DeepSeek Harness',
  [string]$Out = 'D:\量化\reports\window.png'
)

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinCap {
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue |
  Where-Object { $_.MainWindowHandle -ne 0 } |
  Select-Object -First 1
if ($null -eq $proc) { Write-Error "no window for process '$ProcessName'"; exit 2 }

$hwnd = $proc.MainWindowHandle
Write-Host ("window: pid={0} hwnd={1} title={2}" -f $proc.Id, $hwnd, $proc.MainWindowTitle)

[void][WinCap]::ShowWindow($hwnd, 9)   # SW_RESTORE
[void][WinCap]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 700

$rect = New-Object WinCap+RECT
[void][WinCap]::GetWindowRect($hwnd, [ref]$rect)
$w = $rect.Right - $rect.Left
$h = $rect.Bottom - $rect.Top
if ($w -le 0 -or $h -le 0) { Write-Error 'bad window rect'; exit 3 }
Write-Host ("size: {0}x{1}" -f $w, $h)

$bmp = New-Object System.Drawing.Bitmap($w, $h)
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $gfx.GetHdc()
$ok = [WinCap]::PrintWindow($hwnd, $hdc, 2)   # PW_RENDERFULLCONTENT
$gfx.ReleaseHdc($hdc)
$gfx.Dispose()
if (-not $ok) { Write-Warning 'PrintWindow returned false; image may be blank' }

$dir = Split-Path -Parent $Out
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Host ("saved: {0} ({1} bytes)" -f $Out, (Get-Item $Out).Length)
