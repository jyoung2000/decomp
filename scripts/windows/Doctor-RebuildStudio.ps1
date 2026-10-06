<#
.SYNOPSIS
  Environment check for Rebuild Studio on Windows: WebView2, VC++ runtime, tools (sha256 vs docs/dependency-lock.json),
  long-path policy, Session 0, install layout, data dir, controller state.

.DESCRIPTION
  Read-only (except a temporary file to test that the data dir is writable). Every check yields
  status ok | warn | fail | skip | info plus a remedy. Exit code 1 when a check at or above -FailOn failed.
  Checks that need Windows report `skip` on other platforms (this lets the script be unit-tested on Linux).

.PARAMETER Json          Emit the machine-readable report on stdout instead of the table.
.PARAMETER OutFile       Also write the JSON report to this path.
.PARAMETER FailOn        fail (default): exit 1 on any fail. warn: exit 1 on warn or fail. none: always exit 0.
.PARAMETER SkipChecks    Check ids to skip (e.g. session0,webview2). Skipped checks are listed as `skip` - never silently dropped.
.PARAMETER InstallDir    Directory containing rebuild-studio.exe + rebuild-controller.exe. Missing => fail when given explicitly.
.PARAMETER Smoke         Also run `--version` of each tool (10 s timeout each).
.PARAMETER ToolsRoot     Override %LOCALAPPDATA%\RebuildStudio\tools.
.PARAMETER LockPath      Override docs/dependency-lock.json.

Check ids: os, session0, webview2, vcredist, install, signature, data-dir, controller, longpaths, dotnet, tool-<name>
#>
[CmdletBinding()]
param(
    [switch]$Json,
    [string]$OutFile,
    [ValidateSet('fail', 'warn', 'none')][string]$FailOn = 'fail',
    [string[]]$SkipChecks,
    [string]$InstallDir,
    [switch]$Smoke,
    [string]$ToolsRoot,
    [string]$LockPath
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

if ($SkipChecks) { $SkipChecks = @($SkipChecks | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim().ToLowerInvariant() } | Where-Object { $_ }) } else { $SkipChecks = @() }
if (-not $ToolsRoot) { $ToolsRoot = Get-RsToolsRoot }
$DataDir = Get-RsDataDir
$lockFile = Find-RsLockFile $LockPath
$lock = Read-RsLock $lockFile

$results = New-Object System.Collections.ArrayList

function Add-Check([string]$Id, [string]$Status, [string]$Detail, [string]$Remedy = '') {
    if ($SkipChecks -contains $Id.ToLowerInvariant()) { $Status = 'skip'; $Detail = "skipped by -SkipChecks ($Detail)"; $Remedy = '' }
    [void]$results.Add([pscustomobject]@{ id = $Id; status = $Status; detail = $Detail; remedy = $Remedy })
}

function Get-RegValue([string]$Path, [string]$Name) {
    try {
        $p = Get-ItemProperty -LiteralPath $Path -ErrorAction Stop
        if ($p.PSObject.Properties[$Name]) { return $p.$Name }
    } catch { }
    return $null
}

function Invoke-Probe([string]$File, [string[]]$ArgList, [int]$TimeoutSec = 10) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $File
    $psi.Arguments = ($ArgList | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }) -join ' '
    $psi.RedirectStandardOutput = $true; $psi.RedirectStandardError = $true
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true
    $proc = [System.Diagnostics.Process]::Start($psi)
    if (-not $proc.WaitForExit($TimeoutSec * 1000)) { try { $proc.Kill() } catch { }; return @{ ok = $false; text = "timed out after ${TimeoutSec}s" } }
    $text = ($proc.StandardOutput.ReadToEnd() + $proc.StandardError.ReadToEnd()).Trim()
    $first = ($text -split "`r?`n" | Select-Object -First 1)
    return @{ ok = $true; code = $proc.ExitCode; text = $first }
}

# ---- os -------------------------------------------------------------------------------------------------------------
if ($script:RsIsWindows) {
    $os = [System.Environment]::OSVersion.Version
    $arch = if ([System.Environment]::Is64BitOperatingSystem) { 'x64' } else { 'x86' }
    if (-not [System.Environment]::Is64BitOperatingSystem) {
        Add-Check 'os' 'fail' "32-bit Windows $os" 'Rebuild Studio requires 64-bit Windows 10 1809+ / Windows 11.'
    } elseif ($os.Build -lt 17763) {
        Add-Check 'os' 'fail' "Windows build $($os.Build) ($arch)" 'Windows 10 1809 (build 17763) or newer is required (WebView2).'
    } else {
        Add-Check 'os' 'ok' "Windows $($os.Major).$($os.Minor) build $($os.Build) $arch, PowerShell $($PSVersionTable.PSVersion)"
    }
} else {
    Add-Check 'os' 'skip' "not Windows ($([System.Environment]::OSVersion.Platform))" 'Windows-only checks are skipped.'
}

# ---- session 0 ------------------------------------------------------------------------------------------------------
if ($script:RsIsWindows) {
    $sid = [System.Diagnostics.Process]::GetCurrentProcess().SessionId
    if ($sid -eq 0) {
        Add-Check 'session0' 'fail' 'running in Session 0 (service / non-interactive scheduled task)' 'The UI, WebView2, screen capture and previews need an interactive desktop session. Run from a logged-on user session (RDP/console), not from a service.'
    } elseif (-not [System.Environment]::UserInteractive) {
        Add-Check 'session0' 'warn' "session $sid but UserInteractive=false" 'GUI features may be unavailable in this host process.'
    } else {
        Add-Check 'session0' 'ok' "interactive session $sid ($(if ($env:SESSIONNAME) { $env:SESSIONNAME } else { 'n/a' }))"
    }
} else { Add-Check 'session0' 'skip' 'not Windows' }

# ---- webview2 -------------------------------------------------------------------------------------------------------
$sys = $lock.system_prerequisites
if ($script:RsIsWindows) {
    $pv = $null; $where = $null
    foreach ($k in $sys.webview2_evergreen_bootstrapper.runtime_registry_keys) {
        $v = Get-RegValue $k 'pv'
        if ($v -and $v -ne '0.0.0.0') { $pv = $v; $where = $k; break }
    }
    $min = ConvertTo-RsVersion $sys.webview2_evergreen_bootstrapper.minimum_version
    if (-not $pv) {
        Add-Check 'webview2' 'fail' 'WebView2 Evergreen runtime not found in the registry' "Install it: $($sys.webview2_evergreen_bootstrapper.url) (or run Install-RebuildStudio.ps1). Docs: $($sys.webview2_evergreen_bootstrapper.docs)"
    } else {
        $have = ConvertTo-RsVersion $pv
        if ($have -and $min -and $have -lt $min) {
            Add-Check 'webview2' 'fail' "WebView2 runtime $pv is older than the required $min" "Update via Windows Update / Edge update, or re-run the bootstrapper: $($sys.webview2_evergreen_bootstrapper.url)"
        } else {
            Add-Check 'webview2' 'ok' "WebView2 runtime $pv ($where)"
        }
    }
} else { Add-Check 'webview2' 'skip' 'not Windows' }

# ---- vc++ runtime ---------------------------------------------------------------------------------------------------
if ($script:RsIsWindows) {
    $vk = $sys.vc_redist_x64.registry_key
    $installed = Get-RegValue $vk 'Installed'
    $ver = Get-RegValue $vk 'Version'
    $minVc = ConvertTo-RsVersion $sys.vc_redist_x64.minimum_version
    if ($installed -ne 1) {
        Add-Check 'vcredist' 'fail' 'Microsoft Visual C++ 2015-2022 x64 runtime not installed' "Install from $($sys.vc_redist_x64.url)"
    } else {
        $have = ConvertTo-RsVersion ([string]$ver)
        if ($have -and $minVc -and $have -lt $minVc) {
            Add-Check 'vcredist' 'warn' "VC++ runtime $ver is older than $minVc" "Update from $($sys.vc_redist_x64.url)"
        } else { Add-Check 'vcredist' 'ok' "VC++ runtime $ver" }
    }
} else { Add-Check 'vcredist' 'skip' 'not Windows' }

# ---- install layout / signature -------------------------------------------------------------------------------------
$explicitInstall = [bool]$InstallDir
if (-not $InstallDir) {
    $meta = Join-Path $DataDir 'install.json'
    if (Test-Path -LiteralPath $meta) { try { $InstallDir = (Get-Content -LiteralPath $meta -Raw | ConvertFrom-Json).install_dir } catch { } }
}
if (-not $InstallDir -and $script:RsIsWindows) { $InstallDir = Join-Path $env:LOCALAPPDATA 'Programs\RebuildStudio' }
if ($InstallDir) {
    $exe = Join-Path $InstallDir 'rebuild-studio.exe'
    $side = Join-Path $InstallDir 'rebuild-controller.exe'
    $miss = @(@($exe, $side) | Where-Object { -not (Test-Path -LiteralPath $_ -PathType Leaf) })
    if ($miss.Count -gt 0) {
        $st = if ($explicitInstall) { 'fail' } else { 'warn' }
        Add-Check 'install' $st "missing in ${InstallDir}: $(($miss | ForEach-Object { Split-Path -Leaf $_ }) -join ', ')" 'Run Install-RebuildStudio.ps1 (or pass -InstallDir to the build output folder).'
        Add-Check 'signature' 'skip' 'no executable to inspect'
    } else {
        $size = (Get-Item -LiteralPath $side).Length
        if ($size -lt 1MB) { Add-Check 'install' 'fail' "rebuild-controller.exe is only $size bytes (stub, not the packaged controller)" 'Rebuild with Build-RebuildStudio.ps1; never ship the dev stub.' }
        else { Add-Check 'install' 'ok' "$InstallDir (rebuild-studio.exe + rebuild-controller.exe $([math]::Round($size / 1MB, 1)) MB)" }
        if ($script:RsIsWindows) {
            try {
                $sig = Get-AuthenticodeSignature -LiteralPath $exe
                if ($sig.Status -eq 'Valid') { Add-Check 'signature' 'ok' "signed by $($sig.SignerCertificate.Subject)" }
                else { Add-Check 'signature' 'info' "rebuild-studio.exe is UNSIGNED ($($sig.Status)); SmartScreen will warn" 'Expected for CI builds; sign before public distribution (docs/WINDOWS_RELEASE_GATES.md).' }
            } catch { Add-Check 'signature' 'info' "could not read signature: $($_.Exception.Message)" }
        } else { Add-Check 'signature' 'skip' 'not Windows' }
    }
} else {
    Add-Check 'install' 'skip' 'no install directory known'
    Add-Check 'signature' 'skip' 'no install directory known'
}

# ---- data dir / controller ------------------------------------------------------------------------------------------
try {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $probe = Join-Path $DataDir (".doctor-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
    Set-Content -LiteralPath $probe -Value 'x'; Remove-Item -LiteralPath $probe -Force
    $free = $null
    try { $root = [System.IO.Path]::GetPathRoot((Resolve-Path -LiteralPath $DataDir).Path); $free = (New-Object System.IO.DriveInfo($root)).AvailableFreeSpace } catch { }
    if ($free -ne $null -and $free -lt 5GB) { Add-Check 'data-dir' 'warn' "$DataDir writable, only $([math]::Round($free / 1GB, 1)) GB free" 'Analysis evidence/artifacts can be large; free some space or set REBUILD_STUDIO_DATA.' }
    else { Add-Check 'data-dir' 'ok' "$DataDir writable$(if ($free -ne $null) { ", $([math]::Round($free / 1GB, 1)) GB free" })" }
} catch {
    Add-Check 'data-dir' 'fail' "$DataDir not writable: $($_.Exception.Message)" 'Fix permissions or set REBUILD_STUDIO_DATA to a writable folder.'
}

$cj = Join-Path $DataDir 'controller.json'
if (Test-Path -LiteralPath $cj) {
    try {
        $c = Get-Content -LiteralPath $cj -Raw | ConvertFrom-Json
        $alive = $false
        if ($c.pid) { $alive = [bool](Get-Process -Id ([int]$c.pid) -ErrorAction SilentlyContinue) }
        if ($alive) { Add-Check 'controller' 'info' "controller running (pid $($c.pid), port $($c.port))" }
        else { Add-Check 'controller' 'info' "stale controller.json (pid $($c.pid) not running); it is replaced on next start" }
    } catch { Add-Check 'controller' 'warn' "controller.json unreadable: $($_.Exception.Message)" 'Delete the file; the shell recreates it.' }
} else { Add-Check 'controller' 'info' 'controller not running (no controller.json)' }

# ---- long paths -----------------------------------------------------------------------------------------------------
if ($script:RsIsWindows) {
    $lp = Get-RegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem' 'LongPathsEnabled'
    if ($lp -eq 1) { Add-Check 'longpaths' 'ok' 'LongPathsEnabled=1' }
    else { Add-Check 'longpaths' 'warn' "LongPathsEnabled=$(if ($null -eq $lp) { 'unset' } else { $lp })" 'Deep game/app trees exceed 260 chars. As admin: New-ItemProperty -Path HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem -Name LongPathsEnabled -Value 1 -PropertyType DWord -Force  (then sign out/in)' }
} else { Add-Check 'longpaths' 'skip' 'not Windows' }

# ---- tools ----------------------------------------------------------------------------------------------------------
function Test-ToolFiles($Name, $T) {
    $dir = Join-Path $ToolsRoot ([string]$T.install_dir)
    $layout = $T.layout
    $entry = Join-Path $dir (([string]$layout.entry) -replace '/', [System.IO.Path]::DirectorySeparatorChar)
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { return @{ status = 'missing'; detail = "not found: $entry"; path = $entry } }
    $want = Get-RsProp $layout 'entry_sha256' $null
    if (-not $want) { return @{ status = 'unverified'; detail = "present at $entry, but the lock pins no hash for it"; path = $entry } }
    $got = Get-RsSha256 $entry
    if (-not (Test-RsHashEqual $got $want)) { return @{ status = 'mismatch'; detail = "sha256 mismatch for $entry (expected $want, got $got)"; path = $entry } }
    $extra = Get-RsProp $layout 'extra_files' $null
    if ($extra) {
        foreach ($p in $extra.PSObject.Properties) {
            $f = Join-Path $dir ($p.Name -replace '/', [System.IO.Path]::DirectorySeparatorChar)
            if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { return @{ status = 'missing'; detail = "missing companion file $($p.Name)"; path = $entry } }
            if (-not (Test-RsHashEqual (Get-RsSha256 $f) $p.Value)) { return @{ status = 'mismatch'; detail = "sha256 mismatch for companion file $($p.Name)"; path = $entry } }
        }
    }
    return @{ status = 'ok'; detail = "$entry sha256 verified (v$($T.version))"; path = $entry }
}

foreach ($name in @($lock.tools.PSObject.Properties.Name)) {
    if ($name -eq 'dotnet-runtime') { continue }   # covered by the 'dotnet' check
    $t = $lock.tools.$name
    $optional = [bool](Get-RsProp $t 'optional' $false)
    $r = Test-ToolFiles $name $t
    $remedy = "Run: .\Setup-Dependencies.ps1 -Tool $name$(if ($r.status -eq 'mismatch') { ' -Force' })"
    switch ($r.status) {
        'ok' {
            $detail = $r.detail
            if ($Smoke) {
                try {
                    $va = @(Get-RsProp $t.layout 'version_args' @('--version'))
                    $file = $r.path; $args2 = $va
                    if ($name -eq 'ilspycmd') { $file = Join-Path (Split-Path -Parent $r.path) 'ilspycmd.cmd'; $args2 = @('--version') }
                    if ($script:RsIsWindows) {
                        if ($file.EndsWith('.cmd')) { $p = Invoke-Probe "$env:SystemRoot\System32\cmd.exe" (@('/c', $file) + $args2) } else { $p = Invoke-Probe $file $args2 }
                        $detail += "; --version: $($p.text)"
                        if (-not $p.ok) { Add-Check "tool-$name" 'fail' $detail "The binary hangs or crashes. $remedy -Force"; continue }
                    } else { $detail += '; smoke skipped (not Windows)' }
                } catch { Add-Check "tool-$name" 'fail' "$detail; smoke failed: $($_.Exception.Message)" $remedy; continue }
            }
            Add-Check "tool-$name" 'ok' $detail
        }
        'unverified' { Add-Check "tool-$name" 'warn' $r.detail 'Pin entry_sha256 in docs/dependency-lock.json.' }
        'mismatch'   { Add-Check "tool-$name" 'fail' $r.detail "$remedy (refuses unknown binaries)" }
        'missing'    {
            $st = if ($optional) { 'warn' } else { 'fail' }
            Add-Check "tool-$name" $st "$($r.detail)$(if ($optional) { ' (optional)' })" $remedy
        }
    }
}

# ---- dotnet ---------------------------------------------------------------------------------------------------------
$dn = $lock.tools.'dotnet-runtime'
$dnTools = Test-ToolFiles 'dotnet-runtime' $dn
$dnExe = $null
if ($dnTools.status -in @('ok', 'unverified')) { $dnExe = $dnTools.path }
elseif ($dnTools.status -eq 'mismatch') { Add-Check 'dotnet' 'fail' $dnTools.detail 'Run: .\Setup-Dependencies.ps1 -Tool dotnet-runtime -Force' }
if ($dnTools.status -ne 'mismatch') {
    if (-not $dnExe) { $cmd = Get-Command dotnet -ErrorAction SilentlyContinue; if ($cmd) { $dnExe = $cmd.Source } }
    if (-not $dnExe) {
        Add-Check 'dotnet' 'fail' '.NET 8 runtime not found (tools\dotnet or PATH)' 'ilspycmd (managed-code recovery) needs it. Run .\Setup-Dependencies.ps1 -Tool dotnet-runtime (after the lock pins its hash) or install the .NET 8 runtime.'
    } else {
        try {
            $p = & $dnExe --list-runtimes 2>&1 | Out-String
            if ($p -match 'Microsoft\.NETCore\.App 8\.') { Add-Check 'dotnet' 'ok' "$dnExe provides Microsoft.NETCore.App 8.x$(if ($dnTools.status -eq 'unverified') { ' (lock pins no hash for the bundled runtime)' })" }
            else { Add-Check 'dotnet' 'fail' "$dnExe has no Microsoft.NETCore.App 8.x runtime" 'Install the .NET 8 runtime.' }
        } catch { Add-Check 'dotnet' 'fail' "could not run ${dnExe}: $($_.Exception.Message)" }
    }
}

# ---- report ---------------------------------------------------------------------------------------------------------
$counts = @{ ok = 0; warn = 0; fail = 0; skip = 0; info = 0 }
foreach ($r in $results) { $counts[$r.status]++ }
$report = [ordered]@{
    tool = 'Doctor-RebuildStudio'; generated_utc = (Get-Date).ToUniversalTime().ToString('o')
    host = [ordered]@{ os = [string][System.Environment]::OSVersion.VersionString; is_windows = $script:RsIsWindows; powershell = [string]$PSVersionTable.PSVersion }
    lock = $lockFile; data_dir = $DataDir; tools_root = $ToolsRoot
    summary = [ordered]@{ ok = $counts.ok; warn = $counts.warn; fail = $counts.fail; skip = $counts.skip; info = $counts.info }
    checks = @($results)
}
$jsonText = $report | ConvertTo-Json -Depth 6
if ($OutFile) {
    $d = Split-Path -Parent $OutFile
    if ($d -and -not (Test-Path -LiteralPath $d)) { New-Item -ItemType Directory -Force -Path $d | Out-Null }
    Set-Content -LiteralPath $OutFile -Value $jsonText -Encoding UTF8
}
if ($Json) { Write-Output $jsonText }
else {
    foreach ($r in $results) {
        $color = switch ($r.status) { 'ok' { 'Green' } 'warn' { 'Yellow' } 'fail' { 'Red' } default { 'DarkGray' } }
        Write-Host ("[{0,-4}] {1,-14} {2}" -f $r.status.ToUpper(), $r.id, $r.detail) -ForegroundColor $color
        if ($r.remedy -and $r.status -in @('warn', 'fail')) { Write-Host "       -> $($r.remedy)" -ForegroundColor DarkYellow }
    }
    Write-Host ("`nsummary: ok={0} warn={1} fail={2} info={3} skip={4}" -f $counts.ok, $counts.warn, $counts.fail, $counts.info, $counts.skip)
}
if ($FailOn -eq 'fail' -and $counts.fail -gt 0) { exit 1 }
if ($FailOn -eq 'warn' -and ($counts.fail + $counts.warn) -gt 0) { exit 1 }
exit 0
