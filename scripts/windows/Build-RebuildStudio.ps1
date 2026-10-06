<#
.SYNOPSIS
  Clean-checkout Windows build of Rebuild Studio: UI, PyInstaller sidecar, Tauri NSIS installer, portable zip, SBOM, notices.

.DESCRIPTION
  Run from a clean checkout on Windows x64 (PowerShell 5.1 or 7). Steps (each is printed by -DryRun):
    1  preflight      node/npm/python/rust/cargo-tauri versions against docs/dependency-lock.json
    2  ui             npm ci && npm run build            -> ui/dist
    3  sidecar        two venvs (runtime deps; build tools), PyInstaller --onefile
                        desktop/src-tauri/binaries/rebuild-controller-<target>.exe     (Tauri externalBin, entry = rebuild_controller.cli.main)
                        <work>/pyi/dist/rebuild-mcp.exe                                (for Install-Clients.ps1)
                      then a smoke test: `--version`, `serve --port 0 --data-dir <tmp>` must write controller.json and answer /health
    4  tauri          cargo tauri build --target <target> --bundles nsis            -> NSIS installer
    5  portable       zip of the unbundled app dir (rebuild-studio.exe + rebuild-controller.exe + scripts + docs + clients)
    6  sbom           npm sbom (CycloneDX), cargo cyclonedx (or cargo metadata dump), pip-licenses + pip freeze + cyclonedx-py
    7  record         BUILD-INFO.json, SHA256SUMS.txt, NOTICES.md; signature verification; UNSIGNED.txt when not signed

  Output goes to <OutDir> (default <repo>\dist): RebuildStudio-<ver>-x64-setup[-UNSIGNED].exe,
  RebuildStudio-<ver>-win-x64-portable[-UNSIGNED].zip, sbom\, NOTICES.md, BUILD-INFO.json, SHA256SUMS.txt.
  Artifacts are named and marked UNSIGNED unless -SignCert is given AND every signed file verifies as Valid.
  Nothing here certifies the Windows release gates: see docs/WINDOWS_RELEASE_GATES.md.

.PARAMETER DryRun        Print every step and command; touch nothing, run nothing, need no Windows. Safe on Linux with pwsh.
.PARAMETER OutDir        Artifact folder (default <repo>\dist). A `.gitignore` (*) is written into it.
.PARAMETER WorkDir       Venvs and PyInstaller scratch (default <repo>\build\windows). A `.gitignore` (*) is written into it.
.PARAMETER Target        Rust target triple (default x86_64-pc-windows-msvc; the sidecar file name must carry the same triple).
.PARAMETER PythonExe     Python 3.11-3.13 to create the venvs with (default: py -3.12, then python).
.PARAMETER SkipUi/SkipSidecar/SkipTauri/SkipSbom/SkipSmoke   Skip a step (the later steps then reuse what is already on disk).
.PARAMETER Clean         Delete <WorkDir> and <OutDir> first (only folders carrying this script's marker file).
.PARAMETER SignCert      PFX to sign with. Without it the build is unsigned and says so everywhere.
.PARAMETER SignPassword  SecureString password for the PFX (or set $env:RS_SIGN_PASSWORD).
.PARAMETER TimestampUrl  RFC 3161 timestamp server (default http://timestamp.digicert.com).

.EXAMPLE
  .\scripts\windows\Build-RebuildStudio.ps1 -DryRun
  .\scripts\windows\Build-RebuildStudio.ps1
  .\scripts\windows\Build-RebuildStudio.ps1 -SignCert C:\keys\studio.pfx -SignPassword (Read-Host -AsSecureString)
#>
[CmdletBinding()]
param(
    [switch]$DryRun,
    [string]$OutDir,
    [string]$WorkDir,
    [string]$Target = 'x86_64-pc-windows-msvc',
    [string]$PythonExe,
    [switch]$SkipUi,
    [switch]$SkipSidecar,
    [switch]$SkipTauri,
    [switch]$SkipSbom,
    [switch]$SkipSmoke,
    [switch]$Clean,
    [string]$SignCert,
    [System.Security.SecureString]$SignPassword,
    [string]$TimestampUrl = 'http://timestamp.digicert.com'
)

$ErrorActionPreference = 'Stop'
. "$PSScriptRoot\_common.ps1"

$RepoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$UiDir = Join-RsPath @($RepoRoot, 'ui')
$ControllerDir = Join-RsPath @($RepoRoot, 'controller')
$TauriDir = Join-RsPath @($RepoRoot, 'desktop', 'src-tauri')
$BinDir = Join-RsPath @($TauriDir, 'binaries')
if (-not $OutDir) { $OutDir = Join-RsPath @($RepoRoot, 'dist') }
if (-not $WorkDir) { $WorkDir = Join-RsPath @($RepoRoot, 'build', 'windows') }
$SbomDir = Join-RsPath @($OutDir, 'sbom')
$Marker = '.rebuild-studio-build'
$SidecarName = "rebuild-controller-$Target.exe"
$SidecarPath = Join-RsPath @($BinDir, $SidecarName)
$VenvRuntime = Join-RsPath @($WorkDir, 'venv-runtime')
$VenvBuild = Join-RsPath @($WorkDir, 'venv-build')
$PyiRoot = Join-RsPath @($WorkDir, 'pyi')
$PyiDist = Join-RsPath @($PyiRoot, 'dist')
$McpExe = Join-RsPath @($PyiDist, 'rebuild-mcp.exe')
$ReqBuild = Join-RsPath @($PSScriptRoot, 'requirements-build.txt')
$LockFile = Find-RsLockFile (Join-RsPath @($RepoRoot, 'docs', 'dependency-lock.json'))
$Lock = Read-RsLock $LockFile
$Signing = [bool]$SignCert
$StageName = 'RebuildStudio'
$Suffix = if ($Signing) { '' } else { '-UNSIGNED' }

$conf = Get-Content -LiteralPath (Join-RsPath @($TauriDir, 'tauri.conf.json')) -Raw -Encoding UTF8 | ConvertFrom-Json
$Version = [string]$conf.version
$SetupName = "RebuildStudio-$Version-x64-setup$Suffix.exe"
$PortableName = "RebuildStudio-$Version-win-x64-portable$Suffix.zip"
$StepNo = 0

function Step([string]$Title) {
    $script:StepNo++
    Write-RsLog "== step $($script:StepNo): $Title" 'STEP'
}

function Format-Cmd([string]$File, [string[]]$CmdArgs) {
    $parts = @($File) + @($CmdArgs | ForEach-Object { if ($_ -match '[\s;"]') { '"' + $_ + '"' } else { $_ } })
    return ($parts -join ' ')
}

# Run a native command; throws on non-zero exit. In -DryRun only prints it.
function Invoke-Native {
    param([string]$File, [string[]]$CmdArgs = @(), [string]$In = $RepoRoot)
    if ($DryRun) { Write-RsLog "[in $In] $(Format-Cmd $File $CmdArgs)" 'DRY'; return }
    Write-RsLog "[in $In] $(Format-Cmd $File $CmdArgs)" 'INFO'
    Push-Location $In
    try {
        $global:LASTEXITCODE = 0
        & $File @CmdArgs
        if ($LASTEXITCODE -ne 0) { throw "'$File' exited with code $LASTEXITCODE" }
    } finally { Pop-Location }
}

# Run a native command and return its stdout lines (stderr stays on the console). Throws on non-zero exit.
function Get-NativeLines {
    param([string]$File, [string[]]$CmdArgs = @(), [string]$In = $RepoRoot)
    Push-Location $In
    try {
        $global:LASTEXITCODE = 0
        $out = & $File @CmdArgs
        if ($LASTEXITCODE -ne 0) { throw "'$File' exited with code $LASTEXITCODE" }
        return ,@($out | ForEach-Object { [string]$_ })
    } finally { Pop-Location }
}

function Test-Tool([string]$Name) { return [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Note([string]$Text) { if ($DryRun) { Write-RsLog $Text 'DRY' } else { Write-RsLog $Text 'INFO' } }

function Initialize-BuildDir([string]$Dir) {
    if ($DryRun) { Write-RsLog "ensure $Dir (marker $Marker, .gitignore '*')" 'DRY'; return }
    New-Item -ItemType Directory -Force -Path $Dir | Out-Null
    Save-RsText (Join-RsPath @($Dir, $Marker)) "created by Build-RebuildStudio.ps1`n"
    Save-RsText (Join-RsPath @($Dir, '.gitignore')) "*`n"
}

function Remove-BuildDir([string]$Dir) {
    if ($DryRun) { Write-RsLog "remove $Dir if it carries $Marker" 'DRY'; return }
    if (-not (Test-Path -LiteralPath $Dir)) { return }
    if (-not (Test-Path -LiteralPath (Join-RsPath @($Dir, $Marker)))) {
        throw "refusing to -Clean '$Dir': no $Marker file (not created by this script). Delete it yourself or choose another -OutDir/-WorkDir."
    }
    Remove-Item -LiteralPath $Dir -Recurse -Force
}

function Get-VenvPython([string]$Venv) {
    if ($script:RsIsWindows) { return (Join-RsPath @($Venv, 'Scripts', 'python.exe')) }
    return (Join-RsPath @($Venv, 'bin', 'python'))
}

function Resolve-Python {
    if ($PythonExe) { return @{ file = $PythonExe; args = @() } }
    if ((Test-Tool 'py')) {
        $global:LASTEXITCODE = 0
        $null = & py -3.12 -c 'import sys' 2>$null
        if ($LASTEXITCODE -eq 0) { return @{ file = 'py'; args = @('-3.12') } }
    }
    if ((Test-Tool 'python')) { return @{ file = 'python'; args = @() } }
    throw 'No Python found. Install Python 3.12 (https://www.python.org/downloads/windows/) or pass -PythonExe.'
}

function Get-FileSha([string]$Path) { return (Get-RsSha256 $Path) }

# ---------------------------------------------------------------------------------------------------------------------
Write-RsLog "Rebuild Studio $Version build for $Target ($(if ($Signing) { 'SIGNED with ' + $SignCert } else { 'UNSIGNED' }))" 'INFO'
Write-RsLog "repo: $RepoRoot | out: $OutDir | work: $WorkDir | lock: $LockFile" 'INFO'
if ($DryRun) { Write-RsLog 'DRY RUN: nothing is executed or written' 'DRY' }
elseif (-not $script:RsIsWindows) { throw 'Build-RebuildStudio.ps1 builds the Windows app and must run on Windows. Use -DryRun elsewhere (Linux validation: desktop/scripts/check.sh).' }

if ($Signing -and -not $DryRun) {
    if (-not (Test-Path -LiteralPath $SignCert -PathType Leaf)) { throw "-SignCert not found: $SignCert" }
    if ($SignPassword) {
        $bstr = [System.Runtime.InteropServices.Marshal]::SecureStringToBSTR($SignPassword)
        try { $env:RS_SIGN_PASSWORD = [System.Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [System.Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
    }
    $env:RS_SIGN_PFX = (Resolve-Path -LiteralPath $SignCert).Path
    $env:RS_SIGN_TIMESTAMP = $TimestampUrl
}

if ($Clean) { Remove-BuildDir $WorkDir; Remove-BuildDir $OutDir }
Initialize-BuildDir $WorkDir
Initialize-BuildDir $OutDir
if (-not $DryRun) { Set-RsLogFile (Join-RsPath @($OutDir, 'build.log')) }

$tc = $Lock.build_toolchain
$info = [ordered]@{ tools = [ordered]@{} }

# ---- 1 preflight --------------------------------------------------------------------------------------------------
Step 'preflight'
if ($DryRun) {
    Write-RsLog "check: node major >= $($tc.node.major) (lock tested $($tc.node.tested)); npm >= 10 (npm sbom)" 'DRY'
    Write-RsLog "check: python 3.11-3.13 (lock: $($tc.python.version)); rustc + target $Target; cargo-tauri $($tc.tauri_cli.version) ($($tc.tauri_cli.install))" 'DRY'
    Write-RsLog 'check: optional cargo-cyclonedx / git (SBOM fallback and BUILD-INFO commit are recorded as unavailable when absent)' 'DRY'
    $py = @{ file = if ($PythonExe) { $PythonExe } else { 'py -3.12 (or python)' }; args = @() }
} else {
    $nodeV = (Get-NativeLines 'node' @('--version'))[0].Trim().TrimStart('v')
    if ([int]($nodeV.Split('.')[0]) -lt [int]$tc.node.major) { throw "node $nodeV is older than the required major $($tc.node.major). Install Node $($tc.node.major) LTS." }
    $npmV = (Get-NativeLines 'npm' @('--version'))[0].Trim()
    if ([int]($npmV.Split('.')[0]) -lt 10) { throw "npm $npmV is too old for 'npm sbom' (need >= 10)." }
    $py = Resolve-Python
    $pyV = (Get-NativeLines $py.file ($py.args + @('-c', 'import sys;print(chr(46).join(map(str,sys.version_info[:3])))')))[0].Trim()
    $pyMm = [version]($pyV.Split('.')[0..1] -join '.')
    if ($pyMm -lt [version]'3.11' -or $pyMm -ge [version]'3.14') { throw "Python $pyV is outside the supported 3.11-3.13 range (CI uses $($tc.python.version))." }
    $rustV = (Get-NativeLines 'rustc' @('--version'))[0].Trim()
    if (Test-Tool 'rustup') {
        $installed = Get-NativeLines 'rustup' @('target', 'list', '--installed')
        if ($installed -notcontains $Target) { throw "Rust target $Target is not installed. Run: rustup target add $Target" }
    }
    $tauriV = ''
    try { $tauriV = (Get-NativeLines 'cargo' @('tauri', '--version'))[0].Trim() } catch { }
    if ($tauriV -notmatch [regex]::Escape([string]$tc.tauri_cli.version)) {
        throw "cargo-tauri $($tc.tauri_cli.version) is required (found: '$tauriV'). Run: $($tc.tauri_cli.install)"
    }
    $cyclone = $false
    try { $null = Get-NativeLines 'cargo' @('cyclonedx', '--version'); $cyclone = $true } catch { }
    $gitV = ''
    if (Test-Tool 'git') { try { $gitV = (Get-NativeLines 'git' @('--version'))[0].Trim() } catch { } }
    $info.tools = [ordered]@{ node = $nodeV; npm = $npmV; python = $pyV; rustc = $rustV; cargo_tauri = $tauriV; cargo_cyclonedx = $cyclone; git = $gitV }
    Write-RsLog "node $nodeV, npm $npmV, python $pyV, $rustV, $tauriV, cargo-cyclonedx $(if ($cyclone) { 'present' } else { 'absent (cargo metadata fallback)' })" 'INFO'
}

# ---- 2 ui ---------------------------------------------------------------------------------------------------------
Step 'ui: npm ci && npm run build'
if ($SkipUi) { Note 'skipped (-SkipUi); ui/dist must already exist' }
else {
    Invoke-Native 'npm' @('ci') $UiDir
    Invoke-Native 'npm' @('run', 'build') $UiDir
}
if (-not $DryRun -and -not (Test-Path -LiteralPath (Join-RsPath @($UiDir, 'dist', 'index.html')))) { throw 'ui/dist/index.html is missing: the UI build did not run or failed.' }

# ---- 3 sidecar ----------------------------------------------------------------------------------------------------
Step "sidecar: PyInstaller -> $SidecarName"
$pyVenvRuntime = Get-VenvPython $VenvRuntime
$pyVenvBuild = Get-VenvPython $VenvBuild
$freezeFile = Join-RsPath @($WorkDir, 'python-freeze.txt')

# PyInstaller needs the package's non-.py files (store/schema.sql, comparators/web_harness.mjs). controller/pyproject.toml
# does not declare package-data, so an installed copy would lack them: build from the source tree (--paths) and add data explicitly.
function Get-PyiDataArgs {
    $args2 = @()
    $pkgRoot = Join-RsPath @($ControllerDir, 'rebuild_controller')
    if (-not (Test-Path -LiteralPath $pkgRoot)) { return $args2 }
    $skip = @('.py', '.pyc', '.pyo')
    foreach ($f in (Get-ChildItem -LiteralPath $pkgRoot -Recurse -File | Where-Object { $skip -notcontains $_.Extension.ToLowerInvariant() -and $_.FullName -notmatch '__pycache__' })) {
        $rel = $f.DirectoryName.Substring($ControllerDir.TrimEnd('\', '/').Length).TrimStart('\', '/') -replace '\\', '/'
        $args2 += @('--add-data', "$($f.FullName)$([System.IO.Path]::PathSeparator)$rel")
    }
    return $args2
}

# Every controller module by file name. --collect-submodules silently drops any module that fails to import at
# analysis time, and backends are imported by name at runtime, so a transient import error once shipped a
# controller with no recovery backends. Listing the files makes the bundle independent of build-time imports.
function Get-PyiHiddenImportArgs {
    $args2 = @()
    $pkgRoot = Join-RsPath @($ControllerDir, 'rebuild_controller')
    foreach ($f in (Get-ChildItem -LiteralPath $pkgRoot -Recurse -File -Filter '*.py' | Where-Object { $_.FullName -notmatch '__pycache__' })) {
        $rel = $f.FullName.Substring($ControllerDir.TrimEnd('\', '/').Length).TrimStart('\', '/') -replace '\.py$', '' -replace '[\\/]', '.'
        $rel = $rel -replace '\.__init__$', ''
        $args2 += @('--hidden-import', $rel)
    }
    return $args2
}

function Invoke-Pyinstaller([string]$Name, [string]$EntryScript, [string[]]$Extra) {
    $pyiArgs = @('-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--noupx', '--name', $Name,
        '--distpath', $PyiDist, '--workpath', (Join-RsPath @($PyiRoot, 'work', $Name)), '--specpath', (Join-RsPath @($PyiRoot, 'spec')),
        '--paths', $ControllerDir, '--collect-submodules', 'rebuild_controller', '--collect-all', 'lief')
    if ($DryRun) {
        $pyiArgs += @('--add-data', "<each non-.py file under controller/rebuild_controller>$([System.IO.Path]::PathSeparator)<its package dir>")
        $pyiArgs += @('--hidden-import', '<every module under controller/rebuild_controller>')
    } else { $pyiArgs += (Get-PyiDataArgs); $pyiArgs += (Get-PyiHiddenImportArgs) }
    $pyiArgs += $Extra
    $pyiArgs += $EntryScript
    Invoke-Native $pyVenvBuild $pyiArgs $RepoRoot
}

if ($SkipSidecar) {
    Note "skipped (-SkipSidecar); $SidecarPath must already be the real PyInstaller build"
} else {
    $pyArgs0 = @($py.args)
    # pip builds non-editable installs inside the source folder (controller\build\lib, *.egg-info), which would dirty a clean
    # checkout; install the controller's dependencies from a throw-away copy of pyproject.toml + the package instead.
    $ctlStage = Join-RsPath @($WorkDir, 'controller-src')
    if ($DryRun) { Write-RsLog "copy controller\pyproject.toml + controller\rebuild_controller (no __pycache__) -> $ctlStage (pip installs from the copy, never in-tree)" 'DRY' }
    else {
        if (Test-Path -LiteralPath $ctlStage) { Remove-Item -LiteralPath $ctlStage -Recurse -Force }
        New-Item -ItemType Directory -Force -Path $ctlStage | Out-Null
        Copy-Item -LiteralPath (Join-RsPath @($ControllerDir, 'pyproject.toml')) -Destination $ctlStage
        Copy-Item -LiteralPath (Join-RsPath @($ControllerDir, 'rebuild_controller')) -Destination (Join-RsPath @($ctlStage, 'rebuild_controller')) -Recurse
        Get-ChildItem -LiteralPath $ctlStage -Recurse -Directory -Filter '__pycache__' | Remove-Item -Recurse -Force
    }
    # runtime venv: only the controller's dependencies -> its freeze is what ships, and what the SBOM describes.
    Invoke-Native $py.file ($pyArgs0 + @('-m', 'venv', $VenvRuntime))
    Invoke-Native $pyVenvRuntime @('-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip')
    Invoke-Native $pyVenvRuntime @('-m', 'pip', 'install', '--disable-pip-version-check', $ctlStage)
    Invoke-Native $pyVenvRuntime @('-m', 'pip', 'uninstall', '-y', 'rebuild-controller')
    if ($DryRun) { Write-RsLog "capture '$pyVenvRuntime -m pip freeze' -> $freezeFile (constraints for the build venv + SBOM input)" 'DRY' }
    else { Save-RsText $freezeFile ((Get-NativeLines $pyVenvRuntime @('-m', 'pip', 'freeze')) -join "`n") }
    # build venv: same resolved versions (constraints) + PyInstaller and the SBOM tools from requirements-build.txt.
    Invoke-Native $py.file ($pyArgs0 + @('-m', 'venv', $VenvBuild))
    Invoke-Native $pyVenvBuild @('-m', 'pip', 'install', '--disable-pip-version-check', '--upgrade', 'pip')
    Invoke-Native $pyVenvBuild @('-m', 'pip', 'install', '--disable-pip-version-check', '-c', $freezeFile, '-r', $ReqBuild, $ctlStage)
    Invoke-Native $pyVenvBuild @('-m', 'pip', 'uninstall', '-y', 'rebuild-controller')

    Invoke-Pyinstaller 'rebuild-controller' (Join-RsPath @($PSScriptRoot, 'sidecar_entry.py')) @('--collect-submodules', 'uvicorn', '--collect-submodules', 'websockets')
    Invoke-Pyinstaller 'rebuild-mcp' (Join-RsPath @($PSScriptRoot, 'mcp_entry.py')) @('--exclude-module', 'mcp.cli')

    if ($DryRun) {
        Write-RsLog "copy $(Join-RsPath @($PyiDist, 'rebuild-controller.exe')) -> $SidecarPath (replaces the dev stub; never ship a stub)" 'DRY'
    } else {
        $built = Join-RsPath @($PyiDist, 'rebuild-controller.exe')
        if (-not (Test-Path -LiteralPath $built) -or (Get-Item -LiteralPath $built).Length -lt 1MB) { throw "PyInstaller did not produce a real $built" }
        if (-not (Test-Path -LiteralPath $McpExe) -or (Get-Item -LiteralPath $McpExe).Length -lt 1MB) { throw "PyInstaller did not produce a real $McpExe" }
        New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
        Copy-Item -LiteralPath $built -Destination $SidecarPath -Force
    }
}

if ($Signing) {
    $signer = Join-RsPath @($PSScriptRoot, 'Sign-File.ps1')
    $psExe = if ($script:RsIsWindows) { 'powershell.exe' } else { 'pwsh' }
    foreach ($f in @($SidecarPath, $McpExe)) {
        Invoke-Native $psExe @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $signer, '-Path', $f)
    }
}

# Smoke test of the real sidecar: --version, then serve + controller.json + /health with the bearer token, then tree kill.
function Test-Sidecar([string]$Exe) {
    $v = Get-NativeLines $Exe @('--version')
    Write-RsLog "sidecar --version: $($v -join ' ')" 'INFO'
    $tmp = Join-RsPath @($WorkDir, ('smoke-' + [guid]::NewGuid().ToString('N').Substring(0, 8)))
    New-Item -ItemType Directory -Force -Path $tmp | Out-Null
    $outLog = Join-RsPath @($tmp, 'stdout.log'); $errLog = Join-RsPath @($tmp, 'stderr.log')
    $prevData = $env:REBUILD_STUDIO_DATA
    $env:REBUILD_STUDIO_DATA = $tmp
    $proc = Start-Process -FilePath $Exe -ArgumentList @('serve', '--port', '0', '--data-dir', $tmp) -PassThru -WindowStyle Hidden -RedirectStandardOutput $outLog -RedirectStandardError $errLog
    try {
        $cj = Join-RsPath @($tmp, 'controller.json')
        $deadline = (Get-Date).AddSeconds(120)   # one-file PyInstaller extracts on first run; Defender may scan it
        while (-not (Test-Path -LiteralPath $cj)) {
            if ($proc.HasExited) { throw "sidecar exited early with code $($proc.ExitCode): $((Get-Content -LiteralPath $errLog -Tail 15 -ErrorAction SilentlyContinue) -join ' | ')" }
            if ((Get-Date) -gt $deadline) { throw 'sidecar did not write controller.json within 120 s' }
            Start-Sleep -Milliseconds 300
        }
        Start-Sleep -Milliseconds 300
        $c = Get-Content -LiteralPath $cj -Raw | ConvertFrom-Json
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:$($c.port)/health" -Headers @{ Authorization = "Bearer $($c.token)" } -TimeoutSec 15
        if (-not $h.ok) { throw "/health did not report ok: $($h | ConvertTo-Json -Compress)" }
        Write-RsLog "sidecar smoke ok: port $($c.port), pid $($h.pid), version $($h.version)" 'INFO'
        # Every backend module must load inside the frozen build (tools may be missing; the code may not).
        $doc = (Get-NativeLines $Exe @('doctor', '--data-dir', $tmp, '--json')) -join "`n" | ConvertFrom-Json
        $broken = @($doc.backends | Where-Object { $_.title -like '*(failed to load)*' } | ForEach-Object { "$($_.backend_id): $($_.tools[0].detail)" })
        if ($broken.Count -gt 0) { throw "frozen controller cannot load backends: $($broken -join '; ')" }
        Write-RsLog "sidecar doctor ok: $(@($doc.backends).Count) backends load in the frozen build" 'INFO'
    } finally {
        Stop-RsProcessTree $proc.Id
        if ($null -ne $prevData) { $env:REBUILD_STUDIO_DATA = $prevData } else { Remove-Item Env:\REBUILD_STUDIO_DATA -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    }
}
if ($SkipSmoke) { Note 'sidecar smoke test skipped (-SkipSmoke)' }
elseif ($DryRun) { Write-RsLog "smoke: $SidecarName --version; serve --port 0 --data-dir <tmp>; wait for controller.json; GET /health with Bearer token; taskkill /T /F" 'DRY' }
else { Test-Sidecar $SidecarPath }

if (-not $DryRun -and ((-not (Test-Path -LiteralPath $SidecarPath)) -or (Get-Item -LiteralPath $SidecarPath).Length -lt 1MB)) {
    throw "$SidecarPath is missing or is the dev stub; run without -SkipSidecar so PyInstaller builds the real controller."
}

# ---- 4 tauri ------------------------------------------------------------------------------------------------------
Step 'tauri: cargo tauri build (NSIS)'
$cargoTarget = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } else { Join-RsPath @($TauriDir, 'target') }
$releaseDir = Join-RsPath @($cargoTarget, $Target, 'release')
$appExe = Join-RsPath @($releaseDir, 'rebuild-studio.exe')
$nsisDir = Join-RsPath @($releaseDir, 'bundle', 'nsis')
if ($SkipTauri) { Note "skipped (-SkipTauri); expects $appExe and $nsisDir" }
else {
    $env:TAURI_CONFIG = $null
    Remove-Item Env:\TAURI_CONFIG -ErrorAction SilentlyContinue
    if ($Signing) {
        # Tauri signs rebuild-studio.exe, the sidecar and the NSIS installer through this command (%1 = file).
        $cfg = @{ bundle = @{ windows = @{ signCommand = @{ cmd = $(if ($script:RsIsWindows) { 'powershell.exe' } else { 'pwsh' });
            args = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-RsPath @($PSScriptRoot, 'Sign-File.ps1')), '-Path', '%1') } } } }
        $env:TAURI_CONFIG = ($cfg | ConvertTo-Json -Depth 8 -Compress)
        Note "TAURI_CONFIG (signCommand) = $($env:TAURI_CONFIG)"
    }
    Invoke-Native 'cargo' @('tauri', 'build', '--target', $Target, '--bundles', 'nsis', '--', '--locked') $TauriDir
    Remove-Item Env:\TAURI_CONFIG -ErrorAction SilentlyContinue
}

if ($Signing -and -not $DryRun -and (Test-Path -LiteralPath $appExe)) {
    # Tauri's signCommand normally signs the app binary; make sure the portable copy is signed even if it did not.
    if (-not (Get-RsSignatureInfo $appExe).subject) {
        Write-RsLog 'rebuild-studio.exe was not signed by the Tauri bundler; signing it for the portable zip (the NSIS installer is verified separately in step 7)' 'WARN'
        Invoke-Native $(if ($script:RsIsWindows) { 'powershell.exe' } else { 'pwsh' }) @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-RsPath @($PSScriptRoot, 'Sign-File.ps1')), '-Path', $appExe)
    }
}

$setupOut = Join-RsPath @($OutDir, $SetupName)
if ($DryRun) {
    Write-RsLog "copy $nsisDir\*-setup.exe -> $setupOut" 'DRY'
} else {
    $nsisExe = @(Get-ChildItem -LiteralPath $nsisDir -Filter '*-setup.exe' -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending)
    if ($nsisExe.Count -eq 0) { throw "no NSIS installer found in $nsisDir" }
    Copy-Item -LiteralPath $nsisExe[0].FullName -Destination $setupOut -Force
    if (-not (Test-Path -LiteralPath $appExe)) { throw "rebuild-studio.exe not found at $appExe" }
}

# ---- 5 portable ---------------------------------------------------------------------------------------------------
Step "portable zip: $PortableName"
$stage = Join-RsPath @($WorkDir, 'stage', $StageName)
$portableOut = Join-RsPath @($OutDir, $PortableName)
$scriptsToShip = @('_common.ps1', 'Doctor-RebuildStudio.ps1', 'Setup-Dependencies.ps1', 'Install-Clients.ps1', 'Remove-Clients.ps1',
    'Install-RebuildStudio.ps1', 'Uninstall-RebuildStudio.ps1')
if ($DryRun) {
    Write-RsLog "stage $stage : rebuild-studio.exe, rebuild-controller.exe (from $SidecarName), runtime\Scripts\rebuild-mcp.exe," 'DRY'
    Write-RsLog "  scripts\windows\{$($scriptsToShip -join ', ')}, scripts\install-clients.py, clients\**, docs\NOTICES.md, docs\dependency-lock.json," 'DRY'
    Write-RsLog '  NOTICES.md, UNSIGNED.txt (unsigned only), SHA256SUMS.txt (per-file hashes verified by Install-RebuildStudio.ps1)' 'DRY'
    Write-RsLog "zip -> $portableOut (forward-slash entry names, single top folder $StageName\)" 'DRY'
} else {
    if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item -LiteralPath $appExe -Destination (Join-RsPath @($stage, 'rebuild-studio.exe'))
    Copy-Item -LiteralPath $SidecarPath -Destination (Join-RsPath @($stage, 'rebuild-controller.exe'))
    New-Item -ItemType Directory -Force -Path (Join-RsPath @($stage, 'runtime', 'Scripts')) | Out-Null
    Copy-Item -LiteralPath $McpExe -Destination (Join-RsPath @($stage, 'runtime', 'Scripts', 'rebuild-mcp.exe'))
    $sw = Join-RsPath @($stage, 'scripts', 'windows')
    New-Item -ItemType Directory -Force -Path $sw | Out-Null
    foreach ($s in $scriptsToShip) { Copy-Item -LiteralPath (Join-RsPath @($PSScriptRoot, $s)) -Destination $sw }
    Copy-Item -LiteralPath (Join-RsPath @($RepoRoot, 'scripts', 'install-clients.py')) -Destination (Join-RsPath @($stage, 'scripts'))
    Copy-Item -LiteralPath (Join-RsPath @($RepoRoot, 'clients')) -Destination (Join-RsPath @($stage, 'clients')) -Recurse
    New-Item -ItemType Directory -Force -Path (Join-RsPath @($stage, 'docs')) | Out-Null
    Copy-Item -LiteralPath (Join-RsPath @($RepoRoot, 'docs', 'NOTICES.md')) -Destination (Join-RsPath @($stage, 'docs'))
    Copy-Item -LiteralPath $LockFile -Destination (Join-RsPath @($stage, 'docs', 'dependency-lock.json'))
    Copy-Item -LiteralPath (Join-RsPath @($RepoRoot, 'docs', 'NOTICES.md')) -Destination (Join-RsPath @($stage, 'NOTICES.md'))
    if (-not $Signing) {
        Save-RsText (Join-RsPath @($stage, 'UNSIGNED.txt')) ("This Rebuild Studio build is UNSIGNED. Windows SmartScreen will warn on first run.`nVerify the file hashes in SHA256SUMS.txt against the build record. Version $Version.`n")
    }
    # SHA256SUMS.txt covers every file in the package except itself ("<sha256>  <relative path with />").
    $lines = @()
    $stageFull = [System.IO.Path]::GetFullPath($stage).TrimEnd('\', '/')
    foreach ($f in (Get-ChildItem -LiteralPath $stageFull -Recurse -File | Sort-Object FullName)) {
        $lines += ('{0}  {1}' -f (Get-FileSha $f.FullName), ($f.FullName.Substring($stageFull.Length).TrimStart('\', '/') -replace '\\', '/'))
    }
    Save-RsText (Join-RsPath @($stage, 'SHA256SUMS.txt')) (($lines -join "`n") + "`n")
    New-RsZip -SourceDir $stage -ZipPath $portableOut -RootFolder $StageName
    Write-RsLog "portable zip: $portableOut ($([math]::Round((Get-Item -LiteralPath $portableOut).Length / 1MB, 1)) MB)" 'INFO'
}

# ---- 6 sbom + notices ---------------------------------------------------------------------------------------------
Step 'sbom + notices'
$sbomFiles = @()
if ($SkipSbom) { Note 'skipped (-SkipSbom)' }
else {
    if ($DryRun) {
        Write-RsLog "mkdir $SbomDir" 'DRY'
        Write-RsLog "[in $UiDir] npm sbom --sbom-format cyclonedx --sbom-type application --omit dev  > $SbomDir\ui.cdx.json (UTF-8, no BOM)" 'DRY'
        Write-RsLog "[in $TauriDir] cargo cyclonedx --format json --spec-version 1.5 --target $Target --override-filename rebuild-studio.cdx -> $SbomDir\rebuild-studio.cdx.json" 'DRY'
        Write-RsLog "  fallback when cargo-cyclonedx is absent: cargo metadata --format-version 1 --locked --filter-platform $Target > $SbomDir\cargo-metadata.json + copy Cargo.lock" 'DRY'
        Write-RsLog "[venv-build] pip-licenses --python <venv-runtime> --format json --with-urls --with-license-file --no-license-path --output-file $SbomDir\python-licenses.json" 'DRY'
        Write-RsLog "[venv-build] cyclonedx-py environment <venv-runtime python> --of JSON --sv 1.5 -o $SbomDir\controller.cdx.json" 'DRY'
        Write-RsLog "copy $freezeFile -> $SbomDir\python-freeze.txt; copy docs\NOTICES.md -> $OutDir\NOTICES.md" 'DRY'
    } else {
        New-Item -ItemType Directory -Force -Path $SbomDir | Out-Null
        # UI (shipped npm packages only; dev tooling is omitted)
        $ui = Get-NativeLines 'npm' @('sbom', '--sbom-format', 'cyclonedx', '--sbom-type', 'application', '--omit', 'dev') $UiDir
        Save-RsText (Join-RsPath @($SbomDir, 'ui.cdx.json')) ($ui -join "`n"); $sbomFiles += 'ui.cdx.json'
        # Rust shell
        $haveCyclone = [bool]$info.tools['cargo_cyclonedx']
        if ($haveCyclone) {
            Invoke-Native 'cargo' @('cyclonedx', '--format', 'json', '--spec-version', '1.5', '--target', $Target, '--override-filename', 'rebuild-studio.cdx') $TauriDir
            $gen = Join-RsPath @($TauriDir, 'rebuild-studio.cdx.json')
            if (-not (Test-Path -LiteralPath $gen)) { throw "cargo cyclonedx did not write $gen" }
            Move-Item -LiteralPath $gen -Destination (Join-RsPath @($SbomDir, 'rebuild-studio.cdx.json')) -Force
            $sbomFiles += 'rebuild-studio.cdx.json'
        } else {
            Write-RsLog 'cargo-cyclonedx not installed: writing cargo metadata + Cargo.lock instead (install: cargo install cargo-cyclonedx --version 0.5.9 --locked)' 'WARN'
            $meta = Get-NativeLines 'cargo' @('metadata', '--format-version', '1', '--locked', '--filter-platform', $Target) $TauriDir
            Save-RsText (Join-RsPath @($SbomDir, 'cargo-metadata.json')) ($meta -join "`n"); $sbomFiles += 'cargo-metadata.json'
            Copy-Item -LiteralPath (Join-RsPath @($TauriDir, 'Cargo.lock')) -Destination (Join-RsPath @($SbomDir, 'Cargo.lock')) -Force; $sbomFiles += 'Cargo.lock'
        }
        # Python controller (the runtime venv = what PyInstaller bundled, minus PyInstaller itself)
        Copy-Item -LiteralPath $freezeFile -Destination (Join-RsPath @($SbomDir, 'python-freeze.txt')) -Force; $sbomFiles += 'python-freeze.txt'
        $lic = Join-RsPath @($SbomDir, 'python-licenses.json')
        Invoke-Native $pyVenvBuild @('-m', 'piplicenses', '--python', $pyVenvRuntime, '--format', 'json', '--with-urls', '--with-license-file', '--no-license-path', '--output-file', $lic)
        $sbomFiles += 'python-licenses.json'
        $cdxPy = Join-RsPath @($VenvBuild, 'Scripts', 'cyclonedx-py.exe')
        Invoke-Native $cdxPy @('environment', '--of', 'JSON', '--sv', '1.5', '-o', (Join-RsPath @($SbomDir, 'controller.cdx.json')), $pyVenvRuntime)
        $sbomFiles += 'controller.cdx.json'
    }
}
if ($DryRun) { Write-RsLog "copy NOTICES.md -> $OutDir" 'DRY' }
else { Copy-Item -LiteralPath (Join-RsPath @($RepoRoot, 'docs', 'NOTICES.md')) -Destination (Join-RsPath @($OutDir, 'NOTICES.md')) -Force }

# ---- 7 record -----------------------------------------------------------------------------------------------------
Step 'record: signatures, BUILD-INFO.json, SHA256SUMS.txt'
if ($DryRun) {
    if ($Signing) { Write-RsLog 'verify Get-AuthenticodeSignature == Valid for rebuild-controller.exe, rebuild-studio.exe, setup.exe; any other status fails the build' 'DRY' }
    else { Write-RsLog "write $OutDir\UNSIGNED.txt; artifact names carry -UNSIGNED" 'DRY' }
    Write-RsLog "write $OutDir\BUILD-INFO.json (version, commit, tool versions, lock sha256, artifact sha256/size, signature state) and $OutDir\SHA256SUMS.txt" 'DRY'
    Write-RsLog 'DRY RUN complete. Expected outputs: ' 'DRY'
    Write-RsLog "  $setupOut" 'DRY'; Write-RsLog "  $portableOut" 'DRY'; Write-RsLog "  $SbomDir\{ui,controller,rebuild-studio}.cdx.json, python-licenses.json, python-freeze.txt" 'DRY'
    exit 0
}

$sigReport = [ordered]@{}
if ($Signing) {
    $env:RS_SIGN_PASSWORD = $null; Remove-Item Env:\RS_SIGN_PASSWORD -ErrorAction SilentlyContinue
    $bad = @()
    foreach ($f in @($appExe, $SidecarPath, $setupOut, $McpExe)) {
        $si = Get-RsSignatureInfo $f
        $sigReport[(Split-Path -Leaf $f)] = $si
        $trusted = $si.signed -or ($env:RS_SIGN_ALLOW_UNTRUSTED -eq '1' -and $si.subject)
        if (-not $trusted) { $bad += "$(Split-Path -Leaf $f): $($si.status)" }
    }
    if ($bad.Count -gt 0) { throw "signing requested but verification failed for: $($bad -join '; ')" }
} else {
    Save-RsText (Join-RsPath @($OutDir, 'UNSIGNED.txt')) ("These artifacts were built WITHOUT a code-signing certificate and are marked UNSIGNED.`nWindows SmartScreen will warn. Do not distribute them as a release; see docs/WINDOWS_RELEASE_GATES.md (gate W17).`n")
}

$commit = $null; $dirty = $null
if (Test-Tool 'git') {
    try {
        $commit = (Get-NativeLines 'git' @('rev-parse', 'HEAD') $RepoRoot)[0].Trim()
        $dirty = [bool]((Get-NativeLines 'git' @('status', '--porcelain') $RepoRoot) | Where-Object { $_ })
    } catch { }
}
$artifacts = @()
foreach ($a in @($setupOut, $portableOut)) {
    $artifacts += [ordered]@{ file = (Split-Path -Leaf $a); bytes = (Get-Item -LiteralPath $a).Length; sha256 = (Get-FileSha $a) }
}
foreach ($f in @($SidecarPath, $appExe)) {
    $artifacts += [ordered]@{ file = (Split-Path -Leaf $f); bytes = (Get-Item -LiteralPath $f).Length; sha256 = (Get-FileSha $f); note = 'unpackaged component' }
}
$buildInfo = [ordered]@{
    product = 'Rebuild Studio'; version = $Version; target = $Target
    built_utc = (Get-Date).ToUniversalTime().ToString('o')
    signed = [bool]$Signing; signatures = $sigReport
    unsigned_notice = $(if ($Signing) { $null } else { 'UNSIGNED build: SmartScreen warns; not a release candidate until gate W17 passes' })
    git_commit = $commit; git_dirty = $dirty
    tools = $info.tools
    lock = [ordered]@{ path = 'docs/dependency-lock.json'; sha256 = (Get-FileSha $LockFile) }
    artifacts = $artifacts
    sbom = $sbomFiles
    certified = 'No. Windows release gates (docs/WINDOWS_RELEASE_GATES.md) must be run on a Windows machine and recorded separately.'
}
Save-RsText (Join-RsPath @($OutDir, 'BUILD-INFO.json')) (($buildInfo | ConvertTo-Json -Depth 8) + "`n")

$sumLines = @()
$outFull = [System.IO.Path]::GetFullPath($OutDir).TrimEnd('\', '/')
foreach ($f in (Get-ChildItem -LiteralPath $outFull -Recurse -File | Where-Object { $_.Name -notin @('SHA256SUMS.txt', 'build.log', '.gitignore', $Marker) } | Sort-Object FullName)) {
    $sumLines += ('{0}  {1}' -f (Get-FileSha $f.FullName), ($f.FullName.Substring($outFull.Length).TrimStart('\', '/') -replace '\\', '/'))
}
Save-RsText (Join-RsPath @($OutDir, 'SHA256SUMS.txt')) (($sumLines -join "`n") + "`n")

Write-RsLog "DONE ($(if ($Signing) { 'signed' } else { 'UNSIGNED' })). Artifacts in $OutDir" 'INFO'
Write-RsLog "  $SetupName" 'INFO'
Write-RsLog "  $PortableName" 'INFO'
Write-RsLog 'Not certified: run the Windows gates in docs/WINDOWS_RELEASE_GATES.md on a clean machine.' 'WARN'
exit 0
