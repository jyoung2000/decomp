<#
.SYNOPSIS
  Removes the per-user Rebuild Studio install. Projects, evidence, settings and credentials are KEPT unless -RemoveUserData.

.DESCRIPTION
  Always: stops Rebuild Studio processes started from the install folder (whole process trees), removes the Start-menu
  shortcut, the per-user Apps & Features entry, <data>\install.json and the install folder (+ .new / .previous siblings).
  Never (by default): <data dir> (%LOCALAPPDATA%\RebuildStudio: controller.json, SQLite store, evidence, logs, tools, credentials)
  and Windows Credential Manager entries named "RebuildStudio:*". The report lists what was preserved.
  -RemoveTools      deletes only <data>\tools (the downloaded rizin/gdre/ilspycmd/dotnet/node; re-creatable with Setup-Dependencies.ps1).
  -RemoveUserData   deletes <data dir> AND the "RebuildStudio:*" Credential Manager entries. Irreversible: needs -Force or typing DELETE.
                    Case output folders you chose yourself (the "output_root" of a case) are never touched.
  -RemoveClients    runs Remove-Clients.ps1 first (takes the MCP entry/skill out of Claude Code, Codex, Gemini, Hermes configs).
  Works for the portable-script install. An NSIS-installed copy is removed from Apps & Features instead (its uninstaller also keeps
  %LOCALAPPDATA%\RebuildStudio; it only offers to delete the WebView2/window-state folder named after the bundle identifier).

.PARAMETER InstallDir   Default: install.json in the data dir, else %LOCALAPPDATA%\Programs\RebuildStudio.
.PARAMETER DataDir      Default %LOCALAPPDATA%\RebuildStudio (or $env:REBUILD_STUDIO_DATA).
.PARAMETER DryRun       Print what would be removed/kept. Changes nothing.
.NOTES  Exit codes: 0 ok, 1 failed, 2 usage, 3 refused.
#>
[CmdletBinding()]
param(
    [string]$InstallDir,
    [string]$DataDir,
    [switch]$RemoveUserData,
    [switch]$RemoveTools,
    [switch]$RemoveClients,
    [switch]$Force,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

$script:RestoreLocation = $null
function Restore-Location { if ($script:RestoreLocation -and (Test-Path -LiteralPath $script:RestoreLocation)) { Set-Location -LiteralPath $script:RestoreLocation } }
function Fail([string]$Message, [int]$Code = 1) { Write-RsLog $Message 'ERROR'; Restore-Location; exit $Code }
function Act([string]$Message) { if ($DryRun) { Write-RsLog $Message 'DRY' } else { Write-RsLog $Message 'INFO' } }

if (-not $script:RsIsWindows -and -not $DryRun) { Fail 'Uninstall-RebuildStudio.ps1 removes the Windows install; run it on Windows (or use -DryRun).' 2 }
if (-not $DataDir) { $DataDir = Get-RsDataDir }
$DataDir = [System.IO.Path]::GetFullPath($DataDir).TrimEnd('\', '/')

if (-not $InstallDir) {
    $meta = Join-Path $DataDir 'install.json'
    if (Test-Path -LiteralPath $meta) { try { $InstallDir = (Get-Content -LiteralPath $meta -Raw -Encoding UTF8 | ConvertFrom-Json).install_dir } catch { } }
}
if (-not $InstallDir) {
    $base = $env:LOCALAPPDATA
    if (-not $base) { $base = Join-Path $HOME 'AppData/Local' }
    $InstallDir = Join-RsPath @($base, 'Programs', 'RebuildStudio')
}
$InstallDir = [System.IO.Path]::GetFullPath($InstallDir).TrimEnd('\', '/')
if (-not $DryRun -and (Test-Path -LiteralPath $DataDir)) { Set-RsLogFile (Join-RsPath @($DataDir, 'logs', 'uninstall-rebuild-studio.log')) }
Write-RsLog "install dir: $InstallDir | data dir: $DataDir" 'INFO'

# Safety: only ever delete a folder that looks like our install (contains rebuild-studio.exe) and is not a root/profile.
$installExists = Test-Path -LiteralPath $InstallDir
if ($installExists) {
    $leaf = Split-Path -Leaf $InstallDir
    $isRoot = ($InstallDir.Length -le 3) -or ($InstallDir -eq [System.IO.Path]::GetFullPath($HOME).TrimEnd('\', '/'))
    if ($isRoot -or -not (Test-Path -LiteralPath (Join-Path $InstallDir 'rebuild-studio.exe'))) {
        Fail "refusing to delete '$InstallDir': it has no rebuild-studio.exe (not a Rebuild Studio install) or is a drive root/profile folder." 3
    }
}
# The data dir must never be (or contain) the install dir, or deleting one would take the other.
if ($InstallDir.StartsWith($DataDir + '\', [System.StringComparison]::OrdinalIgnoreCase) -and $RemoveUserData) {
    Fail "install dir is inside the data dir ($DataDir); -RemoveUserData would delete the install while it is in use. Choose different folders." 3
}

# Credential Manager entries written by the shell: target "RebuildStudio:<name>".
function Get-CredentialTargets {
    if (-not $script:RsIsWindows) { return @() }
    $targets = @()
    try {
        $out = & "$env:SystemRoot\System32\cmdkey.exe" /list 2>$null
        foreach ($line in $out) {
            $m = [regex]::Match([string]$line, 'Target:\s*(?:LegacyGeneric:target=|Domain:target=)?(RebuildStudio:\S+)')
            if ($m.Success) { $targets += $m.Groups[1].Value }
        }
    } catch { }
    return @($targets | Select-Object -Unique)
}

# ---- confirmation for the destructive switch ---------------------------------------------------------------------------
if ($RemoveUserData -and -not $DryRun -and -not $Force) {
    Write-RsLog "-RemoveUserData will PERMANENTLY delete $DataDir and every 'RebuildStudio:*' Credential Manager entry." 'WARN'
    $answer = Read-Host 'Type DELETE to continue'
    if ($answer -cne 'DELETE') { Fail 'not confirmed; nothing was changed.' 3 }
}

# ---- 1 stop processes -----------------------------------------------------------------------------------------------
if ($script:RsIsWindows -and $installExists) {
    $prefix = $InstallDir + '\'
    $procs = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.ProcessName -in @('rebuild-studio', 'rebuild-controller', 'rebuild-mcp') -and $_.Path -and $_.Path.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) })
    foreach ($p in $procs) {
        Act "stop $($p.ProcessName) (pid $($p.Id)) and its process tree"
        if (-not $DryRun) { Stop-RsProcessTree $p.Id }
    }
    if ($procs.Count -gt 0 -and -not $DryRun) { Start-Sleep -Seconds 1 }
} else { Act 'stop running Rebuild Studio processes from the install folder (none to check on this host)' }

# ---- 2 client packages ----------------------------------------------------------------------------------------------
if ($RemoveClients) {
    $rc = Join-Path $PSScriptRoot 'Remove-Clients.ps1'
    if (-not (Test-Path -LiteralPath $rc)) { Write-RsLog 'Remove-Clients.ps1 not found next to this script; skipped' 'WARN' }
    else {
        Act 'remove client packages (Remove-Clients.ps1 -Client all)'
        try {
            $psExe = if ($script:RsIsWindows) { 'powershell.exe' } else { 'pwsh' }
            $a = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $rc, '-Client', 'all')
            if ($DryRun) { $a += '-DryRun' }
            & $psExe @a
            if ($LASTEXITCODE -ne 0) { Write-RsLog "Remove-Clients.ps1 exited $LASTEXITCODE (continuing)" 'WARN' }
        } catch { Write-RsLog "Remove-Clients.ps1 failed: $($_.Exception.Message) (continuing)" 'WARN' }
    }
}

# ---- 3 shortcut, Apps entry, install dir ----------------------------------------------------------------------------
$lnkPath = $null
if ($env:APPDATA) { $lnkPath = Join-RsPath @($env:APPDATA, 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'Rebuild Studio.lnk') }
if ($lnkPath -and (Test-Path -LiteralPath $lnkPath)) { Act "remove shortcut $lnkPath"; if (-not $DryRun) { Remove-Item -LiteralPath $lnkPath -Force } }
if ($script:RsIsWindows) {
    $key = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\RebuildStudio'
    if (Test-Path -LiteralPath $key) { Act "remove registry entry $key"; if (-not $DryRun) { Remove-Item -LiteralPath $key -Recurse -Force } }
}
if (-not $DryRun) {
    # Never delete the current directory out from under ourselves; put the caller back where it was when we are done.
    $script:RestoreLocation = (Get-Location).Path
    Set-Location -LiteralPath ([System.IO.Path]::GetTempPath())
}
foreach ($d in @($InstallDir, "$InstallDir.new", "$InstallDir.previous")) {
    if (Test-Path -LiteralPath $d) {
        Act "remove $d"
        if (-not $DryRun) {
            try { Remove-Item -LiteralPath $d -Recurse -Force }
            catch { Fail "could not remove $d : $($_.Exception.Message). Close programs using files in it and re-run." 1 }
        }
    }
}
$installJson = Join-Path $DataDir 'install.json'
if (Test-Path -LiteralPath $installJson) { Act "remove $installJson"; if (-not $DryRun) { Remove-Item -LiteralPath $installJson -Force } }

# ---- 4 user data ----------------------------------------------------------------------------------------------------
$creds = @(Get-CredentialTargets)
if ($RemoveUserData) {
    if (Test-Path -LiteralPath $DataDir) {
        Act "DELETE data dir $DataDir"
        if (-not $DryRun) {
            try {
                Set-RsLogFile ([System.IO.Path]::GetTempFileName())   # the log lives inside the folder we are deleting
                Remove-Item -LiteralPath $DataDir -Recurse -Force
            } catch { Fail "could not delete $DataDir : $($_.Exception.Message)" 1 }
        }
    }
    foreach ($t in $creds) {
        Act "DELETE Credential Manager entry $t"
        if (-not $DryRun) { & "$env:SystemRoot\System32\cmdkey.exe" /delete:$t | Out-Null }
    }
} else {
    if ($RemoveTools) {
        $tools = if ($env:REBUILD_STUDIO_TOOLS) { $env:REBUILD_STUDIO_TOOLS } else { Join-Path $DataDir 'tools' }
        if (Test-Path -LiteralPath $tools) { Act "remove downloaded tools $tools"; if (-not $DryRun) { Remove-Item -LiteralPath $tools -Recurse -Force } }
    }
    Write-RsLog 'PRESERVED (not touched):' 'INFO'
    Write-RsLog "  data dir          $DataDir$(if (Test-Path -LiteralPath $DataDir) { '' } else { ' (does not exist)' })  [store, evidence, logs, controller.json, settings$(if (-not $RemoveTools) { ', tools' })]" 'INFO'
    Write-RsLog "  credentials       $($creds.Count) Windows Credential Manager entr$(if ($creds.Count -eq 1) { 'y' } else { 'ies' }) named RebuildStudio:*, plus <data>\credentials" 'INFO'
    Write-RsLog '  case output       any folder you picked as a case output_root' 'INFO'
    Write-RsLog '  to delete all of it later: Uninstall-RebuildStudio.ps1 -RemoveUserData' 'INFO'
}
if ($DryRun) { Write-RsLog 'DRY RUN complete: nothing was changed' 'DRY' }
else { Write-RsLog 'Rebuild Studio uninstalled.' 'INFO' }
Restore-Location
exit 0
