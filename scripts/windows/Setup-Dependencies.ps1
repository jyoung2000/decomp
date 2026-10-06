<#
.SYNOPSIS
  Download, verify and atomically activate the pinned analysis tools (rizin, GDRE, ilspycmd, .NET runtime, optional Node).

.DESCRIPTION
  Reads docs/dependency-lock.json. For every selected tool:
    1. downloads the pinned artifact (https only) into <tools>\.staging\downloads,
    2. verifies its SHA-256 against the lock. Unknown/absent hash  => REFUSED (nothing is installed),
    3. extracts into a staging folder (zip-slip / size guarded) and verifies the entry binary hash from the lock,
    4. activates atomically: the current <tools>\<dir> is renamed to <tools>\.previous\<dir>-<version>, then the staged
       folder is renamed to <tools>\<dir>. If the second rename fails the first is undone.
  The previous version is kept so -Rollback can restore it. Nothing is installed machine-wide; no admin rights needed.

.PARAMETER Tool           Tools to process (rizin, gdre, ilspycmd, dotnet-runtime, node). Default: all non-optional tools.
.PARAMETER IncludeOptional Also process optional tools (node).
.PARAMETER DryRun         Print what would happen (state, URL, expected hash, refusals). No network, no writes.
.PARAMETER Rollback       Swap each selected tool back to the version kept in .previous.
.PARAMETER ComputeHash    Download ONE tool's artifact and print its SHA-256 (for filling a null hash in the lock after you
                          have cross-checked it against the vendor's published value). Installs nothing.
.PARAMETER Force          Reinstall even if the same version is already active and verified.
.PARAMETER ToolsRoot      Default %LOCALAPPDATA%\RebuildStudio\tools (or $env:REBUILD_STUDIO_TOOLS).
.PARAMETER LockPath       Override the lock file location.
.PARAMETER AllowFileUrl   TEST ONLY: accept file:// URLs in the lock (used by the offline unit checks).

.EXAMPLE
  .\Setup-Dependencies.ps1 -DryRun
  .\Setup-Dependencies.ps1 -Tool rizin,gdre,ilspycmd
  .\Setup-Dependencies.ps1 -Rollback -Tool rizin
#>
[CmdletBinding()]
param(
    [string[]]$Tool,
    [switch]$IncludeOptional,
    [switch]$DryRun,
    [switch]$Rollback,
    [string]$ComputeHash,
    [switch]$Force,
    [string]$ToolsRoot,
    [string]$LockPath,
    [switch]$AllowFileUrl
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

# `powershell -File x.ps1 -Tool a,b` delivers one string "a,b"; accept both forms.
if ($Tool) { $Tool = @($Tool | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
if (-not $ToolsRoot) { $ToolsRoot = Get-RsToolsRoot }
$StagingRoot = Join-Path $ToolsRoot '.staging'
$DownloadRoot = Join-Path $StagingRoot 'downloads'
$PreviousRoot = Join-Path $ToolsRoot '.previous'
$StateRoot = Join-Path $ToolsRoot '.state'

if (-not $DryRun) { Set-RsLogFile (Join-Path (Split-Path -Parent $ToolsRoot) 'logs\setup-dependencies.log') }

$lockFile = Find-RsLockFile $LockPath
$lock = Read-RsLock $lockFile
Write-RsLog "lock: $lockFile" 'INFO'
Write-RsLog "tools root: $ToolsRoot" 'INFO'

$allNames = @($lock.tools.PSObject.Properties.Name)

# ---- tool selection -------------------------------------------------------------------------------------------------
if ($ComputeHash) {
    if ($allNames -notcontains $ComputeHash) { Write-RsLog "unknown tool '$ComputeHash' (known: $($allNames -join ', '))" 'ERROR'; exit 2 }
    $selected = @($ComputeHash)
} elseif ($Tool -and $Tool.Count -gt 0) {
    $unknown = @($Tool | Where-Object { $allNames -notcontains $_ })
    if ($unknown.Count -gt 0) { Write-RsLog "unknown tool(s): $($unknown -join ', ') (known: $($allNames -join ', '))" 'ERROR'; exit 2 }
    $selected = @($Tool)
} else {
    $selected = @($allNames | Where-Object {
        $optional = Get-RsProp $lock.tools.$_ 'optional' $false
        $IncludeOptional -or -not $optional
    })
}

# ---- helpers --------------------------------------------------------------------------------------------------------
function Read-State([string]$Name) {
    $p = Join-Path $StateRoot "$Name.json"
    if (Test-Path -LiteralPath $p) { return (Get-Content -LiteralPath $p -Raw -Encoding UTF8 | ConvertFrom-Json) }
    return $null
}

function Write-State([string]$Name, $State) {
    New-Item -ItemType Directory -Force -Path $StateRoot | Out-Null
    $p = Join-Path $StateRoot "$Name.json"
    $tmp = "$p.tmp"
    ($State | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath $tmp -Encoding UTF8
    Move-Item -LiteralPath $tmp -Destination $p -Force
}

function Save-Artifact([string]$Url, [string]$Dest) {
    if ($Url -like 'file://*') {
        if (-not $AllowFileUrl) { throw "file:// URLs are refused (test-only, needs -AllowFileUrl): $Url" }
        Copy-Item -LiteralPath ([System.Uri]$Url).LocalPath -Destination $Dest -Force
        return
    }
    if ($Url -notlike 'https://*') { throw "only https:// download URLs are allowed: $Url" }
    try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
    $oldPref = $ProgressPreference; $ProgressPreference = 'SilentlyContinue'
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile "$Dest.part" -MaximumRedirection 5
        Move-Item -LiteralPath "$Dest.part" -Destination $Dest -Force
    } finally {
        $ProgressPreference = $oldPref
        if (Test-Path -LiteralPath "$Dest.part") { Remove-Item -LiteralPath "$Dest.part" -Force -ErrorAction SilentlyContinue }
    }
}

# Returns a plan object; $plan.Refusal is set when the tool must not be installed.
function Get-Plan([string]$Name) {
    $t = $lock.tools.$Name
    $art = $t.artifact
    $layout = $t.layout
    $refusal = $null
    $sha = Get-RsProp $art 'sha256' $null
    if (-not $sha) {
        $refusal = "no pinned sha256 in the lock (verify_required=$(Get-RsProp $art 'verify_required' $true)). " +
                   "Run: .\Setup-Dependencies.ps1 -ComputeHash $Name, cross-check the value with the vendor, then record it in docs/dependency-lock.json"
    } elseif ($sha -notmatch '^[0-9a-fA-F]{64}$') {
        $refusal = "pinned sha256 is not a 64-hex-digit value"
    }
    $url = [string]$art.url
    if (-not $refusal -and ($url -notlike 'https://*') -and -not ($AllowFileUrl -and ($url -like 'file://*'))) {
        $refusal = "artifact URL is not https"
    }
    $state = Read-State $Name
    $activeVersion = $null
    if ($state) { $activeVersion = Get-RsProp $state 'version' $null }
    $installDir = [string]$t.install_dir
    return [pscustomobject]@{
        Name          = $Name
        Version       = [string]$t.version
        Url           = $url
        ArtifactName  = [string]$art.name
        Sha256        = $sha
        SizeBytes     = Get-RsProp $art 'size_bytes' $null
        Format        = Get-RsProp $art 'format' 'zip'
        ArchiveRoot   = [string](Get-RsProp $layout 'archive_root' '')
        Entry         = [string]$layout.entry
        EntrySha256   = Get-RsProp $layout 'entry_sha256' $null
        ExtraFiles    = Get-RsProp $layout 'extra_files' $null
        InstallDir    = $installDir
        FinalPath     = Join-Path $ToolsRoot $installDir
        ActiveVersion = $activeVersion
        Refusal       = $refusal
    }
}

function Assert-EntryVerified($Plan, [string]$Root) {
    $entry = Join-Path $Root ($Plan.Entry -replace '/', [System.IO.Path]::DirectorySeparatorChar)
    if (-not (Test-Path -LiteralPath $entry -PathType Leaf)) { throw "expected entry binary missing after extraction: $($Plan.Entry)" }
    if ($Plan.EntrySha256) {
        $actual = Get-RsSha256 $entry
        if (-not (Test-RsHashEqual $actual $Plan.EntrySha256)) { throw "entry binary $($Plan.Entry) sha256 mismatch: expected $($Plan.EntrySha256), got $actual" }
    } else {
        Write-RsLog "$($Plan.Name): entry_sha256 not pinned; only the archive hash was verified" 'WARN'
    }
    if ($Plan.ExtraFiles) {
        foreach ($p in $Plan.ExtraFiles.PSObject.Properties) {
            $f = Join-Path $Root ($p.Name -replace '/', [System.IO.Path]::DirectorySeparatorChar)
            if (-not (Test-Path -LiteralPath $f -PathType Leaf)) { throw "expected file missing after extraction: $($p.Name)" }
            if (-not (Test-RsHashEqual (Get-RsSha256 $f) $p.Value)) { throw "$($p.Name) sha256 mismatch" }
        }
    }
}

# Tool-specific post-processing of the staged folder.
function Complete-Staged($Plan, [string]$Staged) {
    if ($Plan.Name -eq 'ilspycmd') {
        # nupkg ships only ilspycmd.dll (framework dependent). The controller discovers an executable named
        # ilspycmd[.cmd] under tools\ilspycmd, so write a shim that prefers the side-by-side runtime.
        $shim = @(
            '@echo off',
            'setlocal',
            'set "RS_TOOLS=%~dp0.."',
            'if exist "%RS_TOOLS%\dotnet\dotnet.exe" (',
            '  set "DOTNET_ROOT=%RS_TOOLS%\dotnet"',
            '  "%RS_TOOLS%\dotnet\dotnet.exe" "%~dp0ilspycmd.dll" %*',
            ') else (',
            '  dotnet "%~dp0ilspycmd.dll" %*',
            ')',
            'exit /b %ERRORLEVEL%'
        ) -join "`r`n"
        Set-Content -LiteralPath (Join-Path $Staged 'ilspycmd.cmd') -Value $shim -Encoding ASCII
    }
}

function Move-Dir([string]$From, [string]$To) {
    # Directory rename on the same volume: atomic on NTFS.
    [System.IO.Directory]::Move($From, $To)
}

function Install-Tool($Plan) {
    New-Item -ItemType Directory -Force -Path $DownloadRoot, $PreviousRoot | Out-Null
    $dl = Join-Path $DownloadRoot $Plan.ArtifactName
    $needDownload = $true
    if (Test-Path -LiteralPath $dl) {
        if (Test-RsHashEqual (Get-RsSha256 $dl) $Plan.Sha256) { Write-RsLog "$($Plan.Name): using cached verified download" 'INFO'; $needDownload = $false }
        else { Remove-Item -LiteralPath $dl -Force }
    }
    if ($needDownload) {
        Write-RsLog "$($Plan.Name): downloading $($Plan.Url)" 'STEP'
        Save-Artifact $Plan.Url $dl
    }
    $actual = Get-RsSha256 $dl
    if (-not (Test-RsHashEqual $actual $Plan.Sha256)) {
        Remove-Item -LiteralPath $dl -Force -ErrorAction SilentlyContinue
        throw "$($Plan.Name): sha256 MISMATCH for $($Plan.ArtifactName): expected $($Plan.Sha256), got $actual. Download deleted, nothing installed."
    }
    if ($Plan.SizeBytes -and ((Get-Item -LiteralPath $dl).Length -ne [long]$Plan.SizeBytes)) {
        throw "$($Plan.Name): size mismatch (expected $($Plan.SizeBytes) bytes)"
    }
    Write-RsLog "$($Plan.Name): archive sha256 verified ($actual)" 'INFO'

    $staged = Join-Path $StagingRoot ("{0}-{1}-{2}" -f $Plan.InstallDir, $Plan.Version, ([guid]::NewGuid().ToString('N').Substring(0, 8)))
    try {
        $n = Expand-RsZipSafe -ZipPath $dl -Destination $staged -OnlyUnder $Plan.ArchiveRoot
        Write-RsLog "$($Plan.Name): extracted $n files" 'INFO'
        Complete-Staged $Plan $staged
        Assert-EntryVerified $Plan $staged
    } catch {
        if (Test-Path -LiteralPath $staged) { Remove-Item -LiteralPath $staged -Recurse -Force -ErrorAction SilentlyContinue }
        throw
    }

    # ---- atomic activation, keeping the previous version -------------------------------------------------------------
    $oldState = Read-State $Plan.Name
    $prevDir = $null; $prevVersion = $null
    if (Test-Path -LiteralPath $Plan.FinalPath) {
        $prevVersion = if ($oldState) { [string](Get-RsProp $oldState 'version' 'unknown') } else { 'unknown-' + (Get-Date -Format 'yyyyMMddHHmmss') }
        $prevDir = Join-Path $PreviousRoot ("{0}-{1}" -f $Plan.InstallDir, $prevVersion)
        if (Test-Path -LiteralPath $prevDir) { Remove-Item -LiteralPath $prevDir -Recurse -Force }
        try { Move-Dir $Plan.FinalPath $prevDir }
        catch { Remove-Item -LiteralPath $staged -Recurse -Force -ErrorAction SilentlyContinue
                throw "$($Plan.Name): cannot move the active version aside ($($_.Exception.Message)). Close running analyses/previews that use it and retry." }
    }
    try { Move-Dir $staged $Plan.FinalPath }
    catch {
        if ($prevDir -and (Test-Path -LiteralPath $prevDir)) { Move-Dir $prevDir $Plan.FinalPath }
        Remove-Item -LiteralPath $staged -Recurse -Force -ErrorAction SilentlyContinue
        throw "$($Plan.Name): activation failed, previous version restored ($($_.Exception.Message))"
    }
    # drop the version that was "previous" before this install (only one is kept)
    if ($oldState) {
        $oldPrev = Get-RsProp $oldState 'previous' $null
        if ($oldPrev -and $oldPrev.dir -and ($oldPrev.dir -ne $prevDir) -and (Test-Path -LiteralPath $oldPrev.dir)) {
            Remove-Item -LiteralPath $oldPrev.dir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $prevObj = $null
    if ($prevDir) { $prevObj = [ordered]@{ version = $prevVersion; dir = $prevDir } }
    Write-State $Plan.Name ([ordered]@{
        name = $Plan.Name; version = $Plan.Version; artifact_sha256 = $Plan.Sha256; entry_sha256 = $Plan.EntrySha256
        activated_utc = (Get-Date).ToUniversalTime().ToString('o'); path = $Plan.FinalPath; previous = $prevObj
    })
    Write-RsLog "$($Plan.Name) $($Plan.Version) active at $($Plan.FinalPath)" 'INFO'
}

function Invoke-Rollback([string]$Name) {
    $t = $lock.tools.$Name
    $installDir = [string]$t.install_dir
    $final = Join-Path $ToolsRoot $installDir
    $state = Read-State $Name
    if (-not $state -or -not (Get-RsProp $state 'previous' $null) -or -not (Test-Path -LiteralPath $state.previous.dir)) {
        Write-RsLog "${Name}: no previous version kept; nothing to roll back" 'WARN'
        return $false
    }
    $prev = $state.previous
    if ($DryRun) { Write-RsLog "${Name}: would roll back $($state.version) -> $($prev.version) ($($prev.dir))" 'DRY'; return $true }
    New-Item -ItemType Directory -Force -Path $PreviousRoot | Out-Null
    $curVersion = [string]$state.version
    $newPrevDir = Join-Path $PreviousRoot ("{0}-{1}" -f $installDir, $curVersion)
    if ($newPrevDir -eq $prev.dir) { throw "${Name}: previous and current resolve to the same folder" }
    if (Test-Path -LiteralPath $newPrevDir) { Remove-Item -LiteralPath $newPrevDir -Recurse -Force }
    if (Test-Path -LiteralPath $final) { Move-Dir $final $newPrevDir }
    try { Move-Dir $prev.dir $final }
    catch { if (Test-Path -LiteralPath $newPrevDir) { Move-Dir $newPrevDir $final }; throw "${Name}: rollback failed, current version restored ($($_.Exception.Message))" }
    Write-State $Name ([ordered]@{
        name = $Name; version = [string]$prev.version; artifact_sha256 = $null; entry_sha256 = $null
        activated_utc = (Get-Date).ToUniversalTime().ToString('o'); path = $final
        previous = [ordered]@{ version = $curVersion; dir = $newPrevDir }; rolled_back_from = $curVersion
    })
    Write-RsLog "$Name rolled back to $($prev.version) (previous: $curVersion kept)" 'INFO'
    return $true
}

# ---- main -----------------------------------------------------------------------------------------------------------
$failed = 0; $refused = 0; $done = 0

if ($ComputeHash) {
    $plan = Get-Plan $ComputeHash
    if ($DryRun) { Write-RsLog "would download $($plan.Url) and print its sha256" 'DRY'; exit 0 }
    New-Item -ItemType Directory -Force -Path $DownloadRoot | Out-Null
    $dl = Join-Path $DownloadRoot ("compute-" + $plan.ArtifactName)
    Save-Artifact $plan.Url $dl
    $h = Get-RsSha256 $dl
    Write-Host ""
    Write-Host "sha256  $h  $($plan.ArtifactName)  ($((Get-Item -LiteralPath $dl).Length) bytes)"
    Write-Host "NOT installed. Compare with the vendor-published value before recording it in docs/dependency-lock.json."
    Remove-Item -LiteralPath $dl -Force
    exit 0
}

foreach ($name in $selected) {
    try {
        if ($Rollback) {
            if (Invoke-Rollback $name) { $done++ }
            continue
        }
        $plan = Get-Plan $name
        $already = ($plan.ActiveVersion -eq $plan.Version) -and (Test-Path -LiteralPath $plan.FinalPath) -and -not $Force
        if ($plan.Refusal) {
            $refused++
            Write-RsLog "$name $($plan.Version): REFUSED - $($plan.Refusal)" 'ERROR'
            continue
        }
        if ($already) {
            Write-RsLog "$name $($plan.Version): already active (use -Force to reinstall)" 'INFO'
            continue
        }
        if ($DryRun) {
            Write-RsLog "$name $($plan.Version): would download $($plan.Url)" 'DRY'
            Write-RsLog "$name : expected sha256 $($plan.Sha256); entry $($plan.Entry); activate at $($plan.FinalPath); current: $(if ($plan.ActiveVersion) { $plan.ActiveVersion } else { 'none' }) (kept as previous)" 'DRY'
            $done++
            continue
        }
        Install-Tool $plan
        $done++
    } catch {
        $failed++
        Write-RsLog "${name}: $($_.Exception.Message)" 'ERROR'
    }
}

Write-RsLog "summary: ok=$done refused=$refused failed=$failed$(if ($DryRun) { ' (dry run)' })" 'INFO'
if ($failed -gt 0) { exit 1 }
if ($refused -gt 0) { exit 3 }
exit 0
