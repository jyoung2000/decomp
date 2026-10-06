<#
.SYNOPSIS
  Per-user install of the Rebuild Studio portable package (no admin rights, nothing machine-wide).

.DESCRIPTION
  Installs the unbundled app directory (rebuild-studio.exe, rebuild-controller.exe, scripts, docs, clients) from the portable zip
  (or from the folder this script ships in) into  %LOCALAPPDATA%\Programs\RebuildStudio  and creates the data folder
  %LOCALAPPDATA%\RebuildStudio . This is the scripted alternative to the NSIS installer; both are per-user.

    1  resolve + validate the package: required files, every file hashed against SHA256SUMS.txt, controller is not the dev stub
    2  WebView2 Evergreen runtime: look it up in the registry (keys from docs/dependency-lock.json). If missing, download Microsoft's
       small bootstrapper from the documented URL (https only), REQUIRE a valid Authenticode signature from "Microsoft Corporation",
       and run it silently. The runtime is never bundled. -SkipWebView2 or -WebView2Installer <offline copy> are available.
    3  refuse while rebuild-studio.exe / rebuild-controller.exe from the target folder are running (-StopRunning ends their process trees)
    4  atomic swap: copy to <InstallDir>.new, rename the current install to <InstallDir>.previous, rename .new into place (undone on failure)
    5  create the data dir, write <data>\install.json (read by Doctor-RebuildStudio.ps1), Start-menu shortcut, per-user "Apps" entry
  The data dir is never modified beyond creating it and writing install.json, so re-running upgrades in place and keeps projects/credentials.

.PARAMETER Source           Folder or .zip of the portable package (default: the folder two levels above this script if it holds rebuild-studio.exe).
.PARAMETER InstallDir       Default %LOCALAPPDATA%\Programs\RebuildStudio.
.PARAMETER DataDir          Default %LOCALAPPDATA%\RebuildStudio (or $env:REBUILD_STUDIO_DATA).
.PARAMETER DryRun           Print what would happen (including the WebView2 decision); change nothing, download nothing.
.PARAMETER SkipWebView2     Do not check/install WebView2 (the app will not start without it).
.PARAMETER WebView2Installer  Path to an already downloaded MicrosoftEdgeWebview2Setup.exe (still signature-checked).
.PARAMETER NoShortcut       No Start-menu shortcut.
.PARAMETER NoUninstallEntry No per-user Apps & Features entry.
.PARAMETER StopRunning      End running Rebuild Studio processes from the install folder instead of refusing.
.PARAMETER Force            Install even if SHA256SUMS.txt is missing (never if it is present and does not match).
.PARAMETER LockPath         Override docs/dependency-lock.json.

.NOTES  Exit codes: 0 ok, 1 failed, 2 usage, 3 refused (bad package / signature / app running).
.EXAMPLE
  .\Install-RebuildStudio.ps1 -DryRun
  .\Install-RebuildStudio.ps1 -Source C:\Downloads\RebuildStudio-0.1.0-win-x64-portable-UNSIGNED.zip
#>
[CmdletBinding()]
param(
    [string]$Source,
    [string]$InstallDir,
    [string]$DataDir,
    [switch]$DryRun,
    [switch]$SkipWebView2,
    [string]$WebView2Installer,
    [switch]$NoShortcut,
    [switch]$NoUninstallEntry,
    [switch]$StopRunning,
    [switch]$Force,
    [string]$LockPath
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

function Fail([string]$Message, [int]$Code = 1) { Write-RsLog $Message 'ERROR'; exit $Code }
function Act([string]$Message) { if ($DryRun) { Write-RsLog $Message 'DRY' } else { Write-RsLog $Message 'INFO' } }

if (-not $script:RsIsWindows -and -not $DryRun) { Fail 'Install-RebuildStudio.ps1 installs the Windows app; run it on Windows (or use -DryRun).' 2 }

if (-not $DataDir) { $DataDir = Get-RsDataDir }
if (-not $InstallDir) {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = Join-Path $HOME 'AppData/Local' }
    $InstallDir = Join-RsPath @($base, 'Programs', 'RebuildStudio')
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\', '/')
$ScriptRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$lockFile = $null
try { $lockFile = Find-RsLockFile $LockPath } catch {
    # Installed layout keeps the lock next to docs\; fall back to the package root.
    $cand = Join-RsPath @($ScriptRoot, 'docs', 'dependency-lock.json')
    if (Test-Path -LiteralPath $cand) { $lockFile = $cand } else { Fail $_.Exception.Message 2 }
}
$lock = Read-RsLock $lockFile
$wv = $lock.system_prerequisites.webview2_evergreen_bootstrapper
if (-not $DryRun) { Set-RsLogFile (Join-RsPath @($DataDir, 'logs', 'install-rebuild-studio.log')) }
Write-RsLog "install dir: $InstallDir | data dir: $DataDir | lock: $lockFile" 'INFO'

# ---- 1 package ------------------------------------------------------------------------------------------------------
$tempRoot = $null
$pkg = $null
if (-not $Source) {
    if (Test-Path -LiteralPath (Join-RsPath @($ScriptRoot, 'rebuild-studio.exe'))) { $Source = $ScriptRoot }
    else { Fail "No -Source given and this script is not inside a package (no rebuild-studio.exe in $ScriptRoot). Pass -Source <portable zip or folder>." 2 }
}
if (-not (Test-Path -LiteralPath $Source)) { Fail "-Source not found: $Source" 2 }
$Source = (Resolve-Path -LiteralPath $Source).Path

if (Test-Path -LiteralPath $Source -PathType Leaf) {
    if ($Source -notmatch '\.zip$') { Fail "-Source file must be a .zip: $Source" 2 }
    $tempRoot = Join-RsPath @([System.IO.Path]::GetTempPath(), ('rs-install-' + [guid]::NewGuid().ToString('N').Substring(0, 8)))
    if ($DryRun) { Write-RsLog "would extract $Source (zip-slip/size guarded) to $tempRoot" 'DRY'; $pkg = $null }
    else {
        [void](Expand-RsZipSafe -ZipPath $Source -Destination $tempRoot)
        $pkg = $tempRoot
        if (-not (Test-Path -LiteralPath (Join-RsPath @($pkg, 'rebuild-studio.exe')))) {
            $subs = @(Get-ChildItem -LiteralPath $tempRoot -Directory)
            if ($subs.Count -eq 1 -and (Test-Path -LiteralPath (Join-RsPath @($subs[0].FullName, 'rebuild-studio.exe')))) { $pkg = $subs[0].FullName }
        }
    }
} else { $pkg = $Source }

function Test-Package([string]$Dir) {
    $problems = @()
    foreach ($rel in @('rebuild-studio.exe', 'rebuild-controller.exe', 'scripts/windows/Doctor-RebuildStudio.ps1', 'scripts/windows/_common.ps1',
            'scripts/windows/Uninstall-RebuildStudio.ps1', 'docs/dependency-lock.json', 'docs/NOTICES.md')) {
        if (-not (Test-Path -LiteralPath (Join-Path $Dir $rel) -PathType Leaf)) { $problems += "missing $rel" }
    }
    $side = Join-Path $Dir 'rebuild-controller.exe'
    if ((Test-Path -LiteralPath $side) -and (Get-Item -LiteralPath $side).Length -lt 1MB) { $problems += 'rebuild-controller.exe is smaller than 1 MB (dev stub, not the packaged controller)' }
    $sums = Join-Path $Dir 'SHA256SUMS.txt'
    if (Test-Path -LiteralPath $sums) {
        $listed = @{}
        foreach ($line in (Get-Content -LiteralPath $sums -Encoding UTF8)) {
            if (-not $line.Trim()) { continue }
            $m = [regex]::Match($line, '^([0-9a-fA-F]{64})\s+(.+)$')
            if (-not $m.Success) { $problems += "bad line in SHA256SUMS.txt: $line"; continue }
            $rel = $m.Groups[2].Value.Trim()
            if ($rel -match '(^|/)\.\.(/|$)' -or [System.IO.Path]::IsPathRooted($rel)) { $problems += "unsafe path in SHA256SUMS.txt: $rel"; continue }
            $listed[$rel] = $true
            $f = Join-Path $Dir ($rel -replace '/', [System.IO.Path]::DirectorySeparatorChar)
            if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { $problems += "listed file missing: $rel"; continue }
            if (-not (Test-RsHashEqual (Get-RsSha256 $f) $m.Groups[1].Value)) { $problems += "sha256 mismatch: $rel" }
        }
        $root = [System.IO.Path]::GetFullPath($Dir).TrimEnd('\', '/')
        foreach ($f in (Get-ChildItem -LiteralPath $root -Recurse -File)) {
            $rel = $f.FullName.Substring($root.Length).TrimStart('\', '/') -replace '\\', '/'
            if ($rel -ne 'SHA256SUMS.txt' -and -not $listed.ContainsKey($rel)) { $problems += "file not covered by SHA256SUMS.txt: $rel" }
        }
    } elseif (-not $Force) { $problems += 'SHA256SUMS.txt is missing (use -Force only for a package you built yourself)' }
    return $problems
}

if ($DryRun -and -not $pkg) { Write-RsLog 'would validate the package (required files, SHA256SUMS.txt, controller size)' 'DRY' }
elseif ($DryRun -and -not (Test-Path -LiteralPath (Join-RsPath @($pkg, 'rebuild-studio.exe')))) { Write-RsLog "package $pkg has no rebuild-studio.exe (a real run would stop here)" 'DRY' }
else {
    $problems = @(Test-Package $pkg)
    if ($problems.Count -gt 0) {
        foreach ($p in $problems) { Write-RsLog "package problem: $p" 'ERROR' }
        if ($tempRoot -and (Test-Path -LiteralPath $tempRoot)) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }
        if ($DryRun) { Write-RsLog 'a real run would refuse to install this package' 'DRY' } else { exit 3 }
    } else { Act "package verified: $pkg" }
}

# ---- 2 WebView2 -----------------------------------------------------------------------------------------------------
function Install-WebView2 {
    $have = Get-RsWebView2Version $lock
    $min = ConvertTo-RsVersion $wv.minimum_version
    if ($have -and (-not $min -or (ConvertTo-RsVersion $have) -ge $min)) { Write-RsLog "WebView2 Evergreen runtime $have present (registry); nothing to do" 'INFO'; return }
    $why = if ($have) { "runtime $have is older than $($wv.minimum_version)" } else { 'runtime not found in the registry' }
    if ($SkipWebView2) { Write-RsLog "WebView2: $why, but -SkipWebView2 was given. Rebuild Studio will not start until it is installed: $($wv.url)" 'WARN'; return }
    if ($DryRun) {
        Write-RsLog "WebView2: $why -> would download $($wv.url) ($($wv.file_name)), require a Valid Authenticode signature whose subject contains '$($wv.authenticode_subject_contains)', run it with /silent /install, and re-check the registry" 'DRY'
        return
    }
    $exe = $WebView2Installer
    if (-not $exe) {
        if ($wv.url -notlike 'https://*') { Fail "WebView2 URL in the lock is not https: $($wv.url)" 3 }
        $dl = Join-RsPath @([System.IO.Path]::GetTempPath(), ('rs-wv2-' + [guid]::NewGuid().ToString('N').Substring(0, 8)))
        New-Item -ItemType Directory -Force -Path $dl | Out-Null
        $exe = Join-Path $dl ([string]$wv.file_name)
        Write-RsLog "WebView2: $why; downloading Microsoft's Evergreen bootstrapper from $($wv.url)" 'INFO'
        try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
        $old = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
        try { Invoke-WebRequest -UseBasicParsing -Uri $wv.url -OutFile $exe -MaximumRedirection 5 } finally { $ProgressPreference = $old }
    } elseif (-not (Test-Path -LiteralPath $exe -PathType Leaf)) { Fail "-WebView2Installer not found: $exe" 2 }
    $sig = Get-AuthenticodeSignature -LiteralPath $exe
    $subject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '' }
    if ($sig.Status -ne 'Valid' -or $subject -notlike "*$($wv.authenticode_subject_contains)*") {
        Fail "WebView2 bootstrapper $exe is not validly signed by '$($wv.authenticode_subject_contains)' (status $($sig.Status), subject '$subject'). Refusing to run it." 3
    }
    Write-RsLog "bootstrapper signature ok: $subject" 'INFO'
    $p = Start-Process -FilePath $exe -ArgumentList @('/silent', '/install') -Wait -PassThru
    Write-RsLog "bootstrapper exit code $($p.ExitCode)" 'INFO'
    $now = Get-RsWebView2Version $lock
    if (-not $now) { Fail "WebView2 still not detected after the bootstrapper ran (exit $($p.ExitCode)). Install it manually: $($wv.url) (docs: $($wv.docs))" 1 }
    Write-RsLog "WebView2 runtime $now installed" 'INFO'
}
Install-WebView2

# ---- 3 running processes --------------------------------------------------------------------------------------------
function Get-AppProcesses {
    if (-not $script:RsIsWindows) { return @() }
    $dirPrefix = $InstallDir + '\'
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -in @('rebuild-studio', 'rebuild-controller', 'rebuild-mcp') -and $_.Path -and $_.Path.StartsWith($dirPrefix, [System.StringComparison]::OrdinalIgnoreCase) })
}
$running = @(Get-AppProcesses)
if ($running.Count -gt 0) {
    if ($StopRunning -and -not $DryRun) {
        foreach ($r in $running) { Write-RsLog "stopping $($r.ProcessName) (pid $($r.Id)) and its children" 'INFO'; Stop-RsProcessTree $r.Id }
        Start-Sleep -Seconds 1
    } elseif ($DryRun) { Write-RsLog "$($running.Count) Rebuild Studio process(es) run from $InstallDir; a real run needs -StopRunning" 'DRY' }
    else { Fail "Rebuild Studio is running from $InstallDir (pids $(($running | ForEach-Object { $_.Id }) -join ', ')). Close it or pass -StopRunning." 3 }
}
$nsisDir = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Rebuild Studio' } else { $null }
if ($nsisDir -and (Test-Path -LiteralPath (Join-Path $nsisDir 'rebuild-studio.exe'))) {
    Write-RsLog "an NSIS-installed copy also exists at $nsisDir. Both can coexist, but use one; uninstall the other from Apps & Features if you want a single install." 'WARN'
}

# ---- 4 atomic swap --------------------------------------------------------------------------------------------------
$newDir = "$InstallDir.new"
$prevDir = "$InstallDir.previous"
if ($DryRun) {
    Write-RsLog "copy package -> $newDir; rename $InstallDir -> $prevDir (if present); rename $newDir -> $InstallDir" 'DRY'
} else {
    if (-not $pkg) { Fail 'internal error: no package directory' 1 }
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $InstallDir) | Out-Null
    if (Test-Path -LiteralPath $newDir) { Remove-Item -LiteralPath $newDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $newDir | Out-Null
    Copy-Item -Path (Join-Path $pkg '*') -Destination $newDir -Recurse -Force
    $hadPrev = $false
    try {
        if (Test-Path -LiteralPath $prevDir) { Remove-Item -LiteralPath $prevDir -Recurse -Force }
        if (Test-Path -LiteralPath $InstallDir) { Rename-Item -LiteralPath $InstallDir -NewName (Split-Path -Leaf $prevDir); $hadPrev = $true }
        Rename-Item -LiteralPath $newDir -NewName (Split-Path -Leaf $InstallDir)
    } catch {
        $err = $_.Exception.Message
        if ($hadPrev -and -not (Test-Path -LiteralPath $InstallDir) -and (Test-Path -LiteralPath $prevDir)) {
            Rename-Item -LiteralPath $prevDir -NewName (Split-Path -Leaf $InstallDir)
            Write-RsLog 'previous install restored' 'WARN'
        }
        Fail "could not activate the new install: $err (is a file in $InstallDir open in another program?)" 1
    }
    Write-RsLog "installed to $InstallDir$(if ($hadPrev) { " (previous kept at $prevDir)" })" 'INFO'
}

# ---- 5 data dir, metadata, shortcut, Apps entry ---------------------------------------------------------------------
$version = $null
if (-not $DryRun) {
    try { $version = (Get-Item -LiteralPath (Join-Path $InstallDir 'rebuild-studio.exe')).VersionInfo.ProductVersion } catch { }
}
if ($DryRun) {
    Write-RsLog "create $DataDir (and logs\) if missing; never touch existing contents" 'DRY'
    Write-RsLog "write $DataDir\install.json {install_dir, version, installed_utc, source}" 'DRY'
    if (-not $NoShortcut) { Write-RsLog 'create Start-menu shortcut "Rebuild Studio" -> rebuild-studio.exe' 'DRY' }
    if (-not $NoUninstallEntry) { Write-RsLog 'create HKCU Apps & Features entry (UninstallString = Uninstall-RebuildStudio.ps1; keeps user data)' 'DRY' }
} else {
    New-Item -ItemType Directory -Force -Path (Join-Path $DataDir 'logs') | Out-Null
    $meta = [ordered]@{ install_dir = $InstallDir; version = $version; installed_utc = (Get-Date).ToUniversalTime().ToString('o'); source = $Source; kind = 'portable-script' }
    Save-RsText (Join-Path $DataDir 'install.json') (($meta | ConvertTo-Json) + "`n")
    if (-not $NoShortcut) {
        try {
            $lnkDir = Join-RsPath @($env:APPDATA, 'Microsoft', 'Windows', 'Start Menu', 'Programs')
            $shell = New-Object -ComObject WScript.Shell
            $lnk = $shell.CreateShortcut((Join-Path $lnkDir 'Rebuild Studio.lnk'))
            $lnk.TargetPath = Join-Path $InstallDir 'rebuild-studio.exe'
            $lnk.WorkingDirectory = $InstallDir
            $lnk.Description = 'Rebuild Studio'
            $lnk.Save()
        } catch { Write-RsLog "could not create the Start-menu shortcut: $($_.Exception.Message)" 'WARN' }
    }
    if (-not $NoUninstallEntry) {
        try {
            $k = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\RebuildStudio'
            New-Item -Path $k -Force | Out-Null
            $uninstall = Join-RsPath @($InstallDir, 'scripts', 'windows', 'Uninstall-RebuildStudio.ps1')
            Set-ItemProperty -Path $k -Name DisplayName -Value 'Rebuild Studio'
            Set-ItemProperty -Path $k -Name DisplayVersion -Value ([string]$version)
            Set-ItemProperty -Path $k -Name Publisher -Value 'Rebuild Studio'
            Set-ItemProperty -Path $k -Name InstallLocation -Value $InstallDir
            Set-ItemProperty -Path $k -Name UninstallString -Value "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$uninstall`""
            Set-ItemProperty -Path $k -Name NoModify -Value 1 -Type DWord
            Set-ItemProperty -Path $k -Name NoRepair -Value 1 -Type DWord
        } catch { Write-RsLog "could not write the Apps & Features entry: $($_.Exception.Message)" 'WARN' }
    }
}
if ($tempRoot -and (Test-Path -LiteralPath $tempRoot)) { Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue }

Write-RsLog 'next steps:' 'INFO'
Write-RsLog "  1. Check the machine:   powershell -File `"$(Join-RsPath @($InstallDir, 'scripts', 'windows', 'Doctor-RebuildStudio.ps1'))`" -InstallDir `"$InstallDir`"" 'INFO'
Write-RsLog "  2. Fetch analysis tools: powershell -File `"$(Join-RsPath @($InstallDir, 'scripts', 'windows', 'Setup-Dependencies.ps1'))`" -DryRun   (then without -DryRun)" 'INFO'
Write-RsLog "  3. Start:               `"$(Join-RsPath @($InstallDir, 'rebuild-studio.exe'))`"" 'INFO'
if ($DryRun) { Write-RsLog 'DRY RUN complete: nothing was changed' 'DRY' }
exit 0
