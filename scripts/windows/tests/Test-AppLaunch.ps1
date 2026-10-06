<#
.SYNOPSIS
  Launch the INSTALLED app on Windows and collect evidence: shell + sidecar start, controller.json, /health with the bearer token,
  main window, and that the sidecar dies when the shell is force-killed (kill-on-close job object) without any taskkill /T.

.DESCRIPTION
  Used by .github/workflows/windows.yml (advisory) and by docs/WINDOWS_RELEASE_GATES.md gates W4 and W10. Uses a throw-away data dir
  (REBUILD_STUDIO_DATA) so nothing of a real profile is touched. It starts a real window: run it in an interactive session.

  Evidence JSON (-EvidenceFile): shell_pid, controller_pid, controller_path, ready_seconds, health, window_seen, window_title,
  sidecar_died_after_shell_kill, cleanup. Exit 0 = every REQUIRED check passed; 1 = a required check failed; 2 = usage.
  Required: both processes start from -InstallDir, controller.json parses, /health answers ok with the token, the unauthenticated
  request is refused (401/403), and no rebuild-controller.exe from -InstallDir survives the shell kill (leftovers are tree-killed and reported).
  Not required unless -RequireWindow: a visible main window (headless CI sessions may not have one).

.PARAMETER InstallDir    Folder with rebuild-studio.exe + rebuild-controller.exe (default %LOCALAPPDATA%\Programs\RebuildStudio).
.PARAMETER TimeoutSec    Wait for controller.json / health (default 120; one-file PyInstaller extracts on first run).
.PARAMETER EvidenceFile  Where to write the JSON evidence.
.PARAMETER RequireWindow Fail when no main window handle appears.
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [int]$TimeoutSec = 120,
    [string]$EvidenceFile,
    [switch]$RequireWindow
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\..\_common.ps1"

if (-not $script:RsIsWindows) { Write-Error 'Test-AppLaunch.ps1 needs Windows.'; exit 2 }
if (-not $InstallDir) { $InstallDir = Join-RsPath @($env:LOCALAPPDATA, 'Programs', 'RebuildStudio') }
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\')
$shellExe = Join-Path $InstallDir 'rebuild-studio.exe'
$sideExe = Join-Path $InstallDir 'rebuild-controller.exe'
foreach ($f in @($shellExe, $sideExe)) { if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { Write-Error "missing $f"; exit 2 } }

$data = Join-RsPath @([System.IO.Path]::GetTempPath(), ('rs-launch-' + [guid]::NewGuid().ToString('N').Substring(0, 8)))
New-Item -ItemType Directory -Force -Path $data | Out-Null
$ev = [ordered]@{ install_dir = $InstallDir; data_dir = $data; started_utc = (Get-Date).ToUniversalTime().ToString('o'); checks = [ordered]@{} }
$failed = @()
function Check([string]$Name, [bool]$Ok, [string]$Detail = '') {
    $ev.checks[$Name] = [ordered]@{ ok = $Ok; detail = $Detail }
    Write-Host ("[{0}] {1} {2}" -f $(if ($Ok) { 'PASS' } else { 'FAIL' }), $Name, $Detail) -ForegroundColor $(if ($Ok) { 'Green' } else { 'Red' })
    if (-not $Ok) { $script:failed += $Name }
}
function Get-InstallProcs { @(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -in @('rebuild-studio', 'rebuild-controller') -and $_.Path -and $_.Path.StartsWith($InstallDir + '\', [System.StringComparison]::OrdinalIgnoreCase) }) }

$already = @(Get-InstallProcs)
if ($already.Count -gt 0) { Write-Error "Rebuild Studio is already running from $InstallDir (pids $(($already | ForEach-Object { $_.Id }) -join ',')). Close it first."; exit 2 }

$prevData = $env:REBUILD_STUDIO_DATA
$env:REBUILD_STUDIO_DATA = $data
$sw = [System.Diagnostics.Stopwatch]::StartNew()
$shell = Start-Process -FilePath $shellExe -PassThru -WorkingDirectory $InstallDir
$ev.shell_pid = $shell.Id
try {
    $cj = Join-Path $data 'controller.json'
    $c = $null; $health = $null
    while ($sw.Elapsed.TotalSeconds -lt $TimeoutSec) {
        if ($shell.HasExited) { break }
        if (Test-Path -LiteralPath $cj) {
            try {
                $c = Get-Content -LiteralPath $cj -Raw | ConvertFrom-Json
                $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($c.port)/health" -Headers @{ Authorization = "Bearer $($c.token)" } -TimeoutSec 5
                if ($health.ok) { break }
            } catch { $health = $null }
        }
        Start-Sleep -Milliseconds 400
    }
    $ev.ready_seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    Check 'shell_started' (-not $shell.HasExited) "pid $($shell.Id)$(if ($shell.HasExited) { ", exited with code $($shell.ExitCode)" })"
    Check 'controller_json' ([bool]$c -and [bool]$c.port -and [bool]$c.token) "$cj"
    Check 'health_ok' ([bool]($health -and $health.ok)) $(if ($health) { "version $($health.version), pid $($health.pid)" } else { 'no /health answer' })
    $ev.health = $health
    if ($c) {
        # The token must be required.
        $code = 0
        try { $null = Invoke-WebRequest -UseBasicParsing -Uri "http://127.0.0.1:$($c.port)/health" -TimeoutSec 5; $code = 200 } catch { if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode } }
        Check 'unauthenticated_refused' ($code -in @(401, 403)) "HTTP $code without token"
        $cp = Get-Process -Id ([int]$c.pid) -ErrorAction SilentlyContinue
        $ev.controller_pid = $c.pid
        if ($cp) { $ev.controller_path = $cp.Path }
        Check 'controller_is_installed_sidecar' ([bool]($cp -and $cp.Path -and ($cp.Path -ieq $sideExe))) "pid $($c.pid) path $(if ($cp) { $cp.Path } else { 'not running' })"
    }
    $shell.Refresh()
    $ev.window_seen = ($shell.MainWindowHandle -ne 0)
    $ev.window_title = $shell.MainWindowTitle
    if ($RequireWindow) { Check 'main_window' $ev.window_seen "title '$($shell.MainWindowTitle)'" }
    else { Write-Host ("[INFO] main window: {0} (title '{1}')" -f $ev.window_seen, $shell.MainWindowTitle) }

    # Kill ONLY the shell (no /T). The kill-on-close job object must take the sidecar down with it.
    if (-not $shell.HasExited) { Stop-Process -Id $shell.Id -Force }
    $deadline = (Get-Date).AddSeconds(15)
    do { Start-Sleep -Milliseconds 300; $left = @(Get-InstallProcs) } while ($left.Count -gt 0 -and (Get-Date) -lt $deadline)
    $ev.sidecar_died_after_shell_kill = ($left.Count -eq 0)
    Check 'sidecar_dies_with_shell' ($left.Count -eq 0) $(if ($left.Count -eq 0) { 'no install-dir process left within 15 s of the shell kill' } else { "still running: $(($left | ForEach-Object { "$($_.ProcessName)#$($_.Id)" }) -join ', ')" })
    foreach ($p in $left) { Stop-RsProcessTree $p.Id }
    $ev.cleanup = if ($left.Count -gt 0) { 'leftover processes were tree-killed by the test' } else { 'nothing to clean' }
} finally {
    foreach ($p in @(Get-InstallProcs)) { Stop-RsProcessTree $p.Id }
    if ($null -ne $prevData) { $env:REBUILD_STUDIO_DATA = $prevData } else { Remove-Item Env:\REBUILD_STUDIO_DATA -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
    if ($EvidenceFile) {
        $ev.finished_utc = (Get-Date).ToUniversalTime().ToString('o')
        $ev.failed_checks = $failed
        Save-RsText $EvidenceFile (($ev | ConvertTo-Json -Depth 6) + "`n")
        Write-Host "evidence: $EvidenceFile"
    }
    Remove-Item -LiteralPath $data -Recurse -Force -ErrorAction SilentlyContinue
}
if ($failed.Count -gt 0) { Write-Host "FAILED: $($failed -join ', ')" -ForegroundColor Red; exit 1 }
Write-Host 'app launch checks passed' -ForegroundColor Green
exit 0
