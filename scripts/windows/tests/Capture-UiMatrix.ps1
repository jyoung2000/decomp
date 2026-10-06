<#
.SYNOPSIS
  Gate W9 helper: resize the running Rebuild Studio window to exact LOGICAL client sizes at the current Windows display scale
  and save a PNG of the client area for each, plus matrix.json describing what really happened.

.DESCRIPTION
  1. Start the installed app yourself and open the view you want to inspect (Overview, Plan, Preview & Test, Comparisons,
     Connections, Settings, ...).
  2. Run this script once per display scale (Settings > System > Display > Scale: 100%, 150%, 200%; no sign-out needed).
  3. Review the PNGs against the checklist in docs/WINDOWS_RELEASE_GATES.md (W9).
  Sizes are logical (what CSS sees): the physical client size is logical x scale. The app declares a 1024x700 logical minimum, so
  smaller requests are reported as `clamped`. A size larger than the monitor's work area at this scale is reported as `fits: false`
  and skipped (for example 2560x1440 logical at 200% needs 5120x2880 physical).
  The script makes itself per-monitor-v2 DPI aware so that every rectangle is in physical pixels, and it never changes the system scale.
  It does not decide pass/fail: a human reads the images. Windows only; run in an interactive session.

.PARAMETER OutDir      Folder for PNGs + matrix.json (default .\evidence\ui-matrix-<timestamp>).
.PARAMETER Sizes       Logical client sizes, "WxH" (default 1024x700, 1920x1080, 2560x1440).
.PARAMETER Label       Added to file names (for example the view name).
.PARAMETER ProcessName Default rebuild-studio.
.PARAMETER SettleMs    Wait after each resize before capturing (default 1500).
#>
[CmdletBinding()]
param(
    [string]$OutDir,
    [string[]]$Sizes = @('1024x700', '1920x1080', '2560x1440'),
    [string]$Label = 'view',
    [string]$ProcessName = 'rebuild-studio',
    [int]$SettleMs = 1500
)

$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { Write-Error 'Capture-UiMatrix.ps1 needs Windows.'; exit 2 }
Add-Type -AssemblyName System.Drawing
Add-Type -AssemblyName System.Windows.Forms
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class RsWin {
    [StructLayout(LayoutKind.Sequential)] public struct RECT { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] public struct POINT { public int X, Y; }
    [DllImport("user32.dll")] public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    [DllImport("user32.dll")] public static extern uint GetDpiForWindow(IntPtr hWnd);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] public static extern bool GetClientRect(IntPtr hWnd, out RECT r);
    [DllImport("user32.dll")] public static extern bool ClientToScreen(IntPtr hWnd, ref POINT p);
    [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr hWnd, IntPtr after, int x, int y, int cx, int cy, uint flags);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr hWnd, int cmd);
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
}
'@
# DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
[void][RsWin]::SetProcessDpiAwarenessContext([IntPtr](-4))

$proc = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowHandle -ne 0 }) | Select-Object -First 1
if (-not $proc) { Write-Error "No running '$ProcessName' with a visible window. Start Rebuild Studio first."; exit 2 }
$h = $proc.MainWindowHandle
if (-not $OutDir) { $OutDir = Join-Path (Get-Location).Path ('evidence\ui-matrix-' + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

[void][RsWin]::ShowWindow($h, 9)          # SW_RESTORE: leave maximized state so the size can be set
[void][RsWin]::SetForegroundWindow($h)
$dpi = [RsWin]::GetDpiForWindow($h)
$scale = $dpi / 96.0
$scalePct = [int][math]::Round($scale * 100)
$rows = @()
Write-Host "window of pid $($proc.Id): dpi $dpi (scale $scalePct%)"

foreach ($s in $Sizes) {
    if ($s -notmatch '^(\d+)x(\d+)$') { Write-Error "bad size '$s' (use WxH)"; exit 2 }
    $lw = [int]$Matches[1]; $lh = [int]$Matches[2]
    $pw = [int][math]::Round($lw * $scale); $ph = [int][math]::Round($lh * $scale)
    $wa = [System.Windows.Forms.Screen]::FromHandle($h).WorkingArea
    $row = [ordered]@{ requested_logical = "${lw}x${lh}"; scale_percent = $scalePct; dpi = $dpi; requested_physical = "${pw}x${ph}"
        work_area_physical = "$($wa.Width)x$($wa.Height)"; fits = $true; clamped = $false; actual_client_physical = $null; actual_client_logical = $null; file = $null }
    # Chrome (frame + title bar) = outer - client, measured now so no style maths is needed.
    $outer = New-Object RsWin+RECT; $client = New-Object RsWin+RECT
    [void][RsWin]::GetWindowRect($h, [ref]$outer); [void][RsWin]::GetClientRect($h, [ref]$client)
    $chromeW = ($outer.Right - $outer.Left) - ($client.Right - $client.Left)
    $chromeH = ($outer.Bottom - $outer.Top) - ($client.Bottom - $client.Top)
    if (($pw + $chromeW) -gt $wa.Width -or ($ph + $chromeH) -gt $wa.Height) {
        $row.fits = $false
        Write-Host ("skip {0}: needs {1}x{2} physical (+frame), work area is {3}x{4}" -f $s, $pw, $ph, $wa.Width, $wa.Height) -ForegroundColor Yellow
        $rows += [pscustomobject]$row; continue
    }
    # SWP_NOZORDER (0x4) | SWP_SHOWWINDOW (0x40); move to the work area's top-left so the whole window is on screen.
    [void][RsWin]::SetWindowPos($h, [IntPtr]::Zero, $wa.Left, $wa.Top, $pw + $chromeW, $ph + $chromeH, 0x44)
    Start-Sleep -Milliseconds $SettleMs
    [void][RsWin]::SetForegroundWindow($h)
    Start-Sleep -Milliseconds 300
    $client2 = New-Object RsWin+RECT
    [void][RsWin]::GetClientRect($h, [ref]$client2)
    $cw = $client2.Right - $client2.Left; $ch = $client2.Bottom - $client2.Top
    $row.actual_client_physical = "${cw}x${ch}"
    $row.actual_client_logical = ("{0}x{1}" -f [int][math]::Round($cw / $scale), [int][math]::Round($ch / $scale))
    $row.clamped = ([math]::Abs($cw - $pw) -gt 2 -or [math]::Abs($ch - $ph) -gt 2)
    $pt = New-Object RsWin+POINT
    [void][RsWin]::ClientToScreen($h, [ref]$pt)
    $bmp = New-Object System.Drawing.Bitmap($cw, $ch)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    try { $g.CopyFromScreen($pt.X, $pt.Y, 0, 0, (New-Object System.Drawing.Size($cw, $ch))) } finally { $g.Dispose() }
    $file = "ui-$Label-${lw}x${lh}-${scalePct}pct.png"
    $bmp.Save((Join-Path $OutDir $file), [System.Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
    $row.file = $file
    Write-Host ("captured {0}: client {1} physical = {2} logical{3}" -f $file, $row.actual_client_physical, $row.actual_client_logical, $(if ($row.clamped) { ' (CLAMPED by the window minimum)' } else { '' }))
    $rows += [pscustomobject]$row
}

$matrixFile = Join-Path $OutDir 'matrix.json'
$existing = @()
if (Test-Path -LiteralPath $matrixFile) { $existing = @(Get-Content -LiteralPath $matrixFile -Raw | ConvertFrom-Json) }
$all = @($existing) + @($rows | ForEach-Object { $_ | Add-Member -NotePropertyName label -NotePropertyValue $Label -PassThru -Force })
[System.IO.File]::WriteAllText($matrixFile, ($all | ConvertTo-Json -Depth 5), (New-Object System.Text.UTF8Encoding($false)))
Write-Host "wrote $matrixFile"
exit 0
