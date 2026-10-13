# Grab the screen region occupied by a window (accurate under display scaling / CSS zoom).
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
public class WinGrab {
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);
  [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT lpRect);
  [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left; public int Top; public int Right; public int Bottom; }
}
"@
$proc = Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 } | Select-Object -First 1
if ($null -eq $proc) { Write-Error 'no window'; exit 2 }
$hwnd = $proc.MainWindowHandle
[void][WinGrab]::ShowWindow($hwnd, 9)
[void][WinGrab]::SetForegroundWindow($hwnd)
Start-Sleep -Milliseconds 800
if (-not $NoReload) { [System.Windows.Forms.SendKeys]::SendWait('^r'); Write-Host 'reloaded, waiting 9s'; Start-Sleep -Seconds 9; [void][WinGrab]::SetForegroundWindow($hwnd); Start-Sleep -Milliseconds 800 }
$r = New-Object WinGrab+RECT
[void][WinGrab]::GetWindowRect($hwnd, [ref]$r)
$w = $r.Right - $r.Left; $h = $r.Bottom - $r.Top
$bmp = New-Object System.Drawing.Bitmap($w, $h)
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($r.Left, $r.Top, 0, 0, (New-Object System.Drawing.Size($w, $h)))
$g.Dispose()
$dir = Split-Path -Parent $Out
if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
$bmp.Save($Out, [System.Drawing.Imaging.ImageFormat]::Png)
$bmp.Dispose()
Write-Host ("title={0}; rect={1},{2} {3}x{4}; saved={5}" -f $proc.MainWindowTitle, $r.Left, $r.Top, $w, $h, $Out)
