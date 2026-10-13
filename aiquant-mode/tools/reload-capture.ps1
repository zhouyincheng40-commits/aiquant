# Reload the DSH app window (Ctrl+R) then capture it to PNG.
param(
  [string]$ProcessName = 'DeepSeek Harness',
  [string]$Out = 'D:\path\shot.png',
  [switch]$NoReload
)
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class WinAct {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr hWnd, IntPtr hdcBlt, uint nFlags);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@

$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($null -eq $proc) { Write-Error "no window"; exit 2 }
$hwnd = $proc.MainWindowHandle

if (-not $NoReload) {
  [void][WinAct]::ShowWindow($hwnd, 9)
  [void][WinAct]::SetForegroundWindow($hwnd)
  Start-Sleep -Milliseconds 600
  [System.Windows.Forms.SendKeys]::SendWait('^r')
  Write-Host 'sent Ctrl+R (reload), waiting 8s ...'
  Start-Sleep -Seconds 8
  [void][WinAct]::SetForegroundWindow($hwnd)
  Start-Sleep -Milliseconds 500
}

$rect = New-Object WinAct+RECT
[void][WinAct]::GetWindowRect($hwnd, [ref]$rect)
$w = $rect.Right - $rect.Left; $h = $rect.Bottom - $rect.Top
$bmp = New-Object System.Drawing.Bitmap($w, $h)
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$hdc = $gfx.GetHdc()
$ok = [WinAct]::PrintWindow($hwnd, $hdc, 2)
$gfx.ReleaseHdc($hdc); $gfx.Dispose()
$dir = Split-Path -Parent $Out
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Host ("title={0}; size={1}x{2}; printWindow={3}; saved={4}" -f $proc.MainWindowTitle, $w, $h, $ok, $Out)
