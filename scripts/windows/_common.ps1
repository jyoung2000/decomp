# Shared helpers for the Rebuild Studio Windows scripts. Dot-source: . "$PSScriptRoot\_common.ps1"
# Compatible with Windows PowerShell 5.1 and PowerShell 7+. No external modules.

Set-StrictMode -Version 2.0

$script:RsIsWindows = ($env:OS -eq 'Windows_NT')
$script:RsLogFile = $null

function Get-RsDataDir {
    if ($env:REBUILD_STUDIO_DATA) { return $env:REBUILD_STUDIO_DATA }
    if ($script:RsIsWindows) {
        $base = $env:LOCALAPPDATA
        if (-not $base) { $base = Join-Path $env:USERPROFILE 'AppData\Local' }
        return (Join-Path $base 'RebuildStudio')
    }
    # Non-Windows is only used by the Linux unit checks of these scripts.
    $xdg = $env:XDG_DATA_HOME
    if (-not $xdg) { $xdg = Join-Path $HOME '.local/share' }
    return (Join-Path $xdg 'rebuild-studio')
}

function Get-RsToolsRoot {
    if ($env:REBUILD_STUDIO_TOOLS) { return $env:REBUILD_STUDIO_TOOLS }
    return (Join-Path (Get-RsDataDir) 'tools')
}

function Set-RsLogFile([string]$Path) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    $script:RsLogFile = $Path
}

function Write-RsLog {
    param([string]$Message, [ValidateSet('INFO', 'WARN', 'ERROR', 'STEP', 'DRY')][string]$Level = 'INFO')
    $stamp = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    $line = "[$stamp] [$Level] $Message"
    switch ($Level) {
        'ERROR' { Write-Host $line -ForegroundColor Red }
        'WARN'  { Write-Host $line -ForegroundColor Yellow }
        'STEP'  { Write-Host $line -ForegroundColor Cyan }
        'DRY'   { Write-Host $line -ForegroundColor DarkGray }
        default { Write-Host $line }
    }
    if ($script:RsLogFile) { try { Add-Content -LiteralPath $script:RsLogFile -Value $line -Encoding UTF8 } catch { } }
}

function Find-RsLockFile([string]$Explicit) {
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    $candidates += (Join-Path $PSScriptRoot 'dependency-lock.json')
    $candidates += (Join-Path $PSScriptRoot '..\..\docs\dependency-lock.json')
    $candidates += (Join-Path $PSScriptRoot '..\dependency-lock.json')
    foreach ($c in $candidates) {
        if (Test-Path -LiteralPath $c -PathType Leaf) { return (Resolve-Path -LiteralPath $c).Path }
    }
    throw "dependency-lock.json not found. Looked in: $($candidates -join '; '). Pass -LockPath."
}

function Read-RsLock([string]$Path) {
    $lock = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $lock.PSObject.Properties['schema_version'] -or $lock.schema_version -ne 1) {
        throw "Unsupported dependency-lock.json schema_version in $Path"
    }
    return $lock
}

function Get-RsSha256([string]$Path) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $bytes = $sha.ComputeHash($stream)
        return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '')
    } finally { $stream.Dispose(); $sha.Dispose() }
}

function Test-RsHashEqual([string]$A, [string]$B) {
    if (-not $A -or -not $B) { return $false }
    return ($A.Trim().ToLowerInvariant() -eq $B.Trim().ToLowerInvariant())
}

function Get-RsProp($Object, [string]$Name, $Default = $null) {
    if ($null -ne $Object -and $Object.PSObject.Properties[$Name]) { return $Object.$Name }
    return $Default
}

# Extract a zip (also .nupkg) with zip-slip, entry-count and expansion-size guards.
# -OnlyUnder 'a/b' extracts only entries below that folder and strips it from the destination path.
function Expand-RsZipSafe {
    param(
        [Parameter(Mandatory)][string]$ZipPath,
        [Parameter(Mandatory)][string]$Destination,
        [string]$OnlyUnder = '',
        [long]$MaxBytes = 4GB,
        [int]$MaxEntries = 50000
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    $destFull = [System.IO.Path]::GetFullPath($Destination).TrimEnd('\', '/') + [System.IO.Path]::DirectorySeparatorChar
    $prefix = ''
    if ($OnlyUnder) { $prefix = ($OnlyUnder -replace '\\', '/').Trim('/') + '/' }
    $zip = [System.IO.Compression.ZipFile]::OpenRead($ZipPath)
    $total = 0L; $count = 0; $extracted = 0
    try {
        foreach ($e in $zip.Entries) {
            $count++
            if ($count -gt $MaxEntries) { throw "archive has more than $MaxEntries entries" }
            $total += $e.Length
            if ($total -gt $MaxBytes) { throw "archive expands beyond $MaxBytes bytes" }
            $name = ($e.FullName -replace '\\', '/')
            if ($prefix) {
                if (-not $name.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { continue }
                $name = $name.Substring($prefix.Length)
            }
            if ([string]::IsNullOrEmpty($name)) { continue }
            $target = [System.IO.Path]::GetFullPath((Join-Path $Destination $name))
            if (-not $target.StartsWith($destFull, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "refusing archive entry that escapes the destination: $($e.FullName)"
            }
            if ($name.EndsWith('/')) { New-Item -ItemType Directory -Force -Path $target | Out-Null; continue }
            $parent = Split-Path -Parent $target
            if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Force -Path $parent | Out-Null }
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
            $extracted++
        }
    } finally { $zip.Dispose() }
    if ($extracted -eq 0) { throw "no files extracted from $ZipPath (OnlyUnder='$OnlyUnder')" }
    return $extracted
}

function ConvertTo-RsVersion([string]$Text) {
    if (-not $Text) { return $null }
    $m = [regex]::Match($Text, '(\d+)(\.\d+){1,3}')
    if (-not $m.Success) { return $null }
    $parts = $m.Value.Split('.')
    while ($parts.Count -lt 3) { $parts += '0' }
    try { return [version]($parts -join '.') } catch { return $null }
}

function Test-RsInSession0 {
    if (-not $script:RsIsWindows) { return $false }
    try { return ([System.Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) } catch { return $false }
}

# Kill a process and all its children (Windows: taskkill /T /F).
function Stop-RsProcessTree([int]$ProcessId) {
    if ($script:RsIsWindows) {
        & "$env:SystemRoot\System32\taskkill.exe" /PID $ProcessId /T /F 2>&1 | Out-Null
    } else {
        Stop-Process -Id $ProcessId -Force -ErrorAction SilentlyContinue
    }
}

# ---- helpers shared by the build / install / uninstall scripts (added with M15) -----------------------------------------

# Path.Combine without the Windows-only '\' assumption, so -DryRun also runs under PowerShell 7 on Linux.
function Join-RsPath([string[]]$Parts) {
    return [System.IO.Path]::Combine($Parts)
}

# Highest installed WebView2 Evergreen runtime version string, or $null. Registry keys come from the lock
# (system_prerequisites.webview2_evergreen_bootstrapper.runtime_registry_keys); a version of 0.0.0.0 means "uninstalled".
function Get-RsWebView2Version($Lock) {
    if (-not $script:RsIsWindows) { return $null }
    $best = $null
    foreach ($k in @($Lock.system_prerequisites.webview2_evergreen_bootstrapper.runtime_registry_keys)) {
        try {
            $p = Get-ItemProperty -LiteralPath $k -ErrorAction Stop
            if ($p.PSObject.Properties['pv']) {
                $v = [string]$p.pv
                if ($v -and $v -ne '0.0.0.0') {
                    $cur = ConvertTo-RsVersion $v
                    if ($cur -and (-not $best -or $cur -gt (ConvertTo-RsVersion $best))) { $best = $v }
                }
            }
        } catch { }
    }
    return $best
}

# Write text as UTF-8 without BOM (Windows PowerShell 5.1's Set-Content/Out-File would add a BOM or use UTF-16).
function Save-RsText([string]$Path, [string]$Text) {
    $dir = Split-Path -Parent $Path
    if ($dir -and -not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

# Zip a directory with forward-slash entry names (Windows PowerShell 5.1 Compress-Archive / ZipFile.CreateFromDirectory
# write backslashes on older .NET Framework builds, which breaks extraction on other tools).
function New-RsZip {
    param([Parameter(Mandatory)][string]$SourceDir, [Parameter(Mandatory)][string]$ZipPath, [string]$RootFolder = '')
    Add-Type -AssemblyName System.IO.Compression
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    if (Test-Path -LiteralPath $ZipPath) { Remove-Item -LiteralPath $ZipPath -Force }
    $srcFull = [System.IO.Path]::GetFullPath($SourceDir).TrimEnd('\', '/')
    $zip = [System.IO.Compression.ZipFile]::Open($ZipPath, [System.IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($f in (Get-ChildItem -LiteralPath $srcFull -Recurse -File -Force | Sort-Object FullName)) {
            $rel = $f.FullName.Substring($srcFull.Length).TrimStart('\', '/') -replace '\\', '/'
            if ($RootFolder) { $rel = "$RootFolder/$rel" }
            [void][System.IO.Compression.ZipFileExtensions]::CreateEntryFromFile($zip, $f.FullName, $rel, [System.IO.Compression.CompressionLevel]::Optimal)
        }
    } finally { $zip.Dispose() }
}

# Authenticode signature summary: @{ status; subject; signed }. Non-Windows => status 'NotApplicable'.
function Get-RsSignatureInfo([string]$Path) {
    if (-not $script:RsIsWindows) { return @{ status = 'NotApplicable'; subject = $null; signed = $false } }
    $s = Get-AuthenticodeSignature -LiteralPath $Path
    $subj = $null
    if ($s.SignerCertificate) { $subj = $s.SignerCertificate.Subject }
    return @{ status = [string]$s.Status; subject = $subj; signed = ($s.Status -eq 'Valid') }
}
