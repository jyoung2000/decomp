<#
  Offline checks for Setup-Dependencies.ps1 (hash refusal, atomic activate, previous kept, rollback, zip-slip, dry run).
  Runs on Windows PowerShell 5.1, PowerShell 7 on Windows, and PowerShell 7 on Linux (CI: linux.yml).
  Usage: pwsh -NoProfile -File scripts/windows/tests/Test-SetupDependencies.ps1
#>
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression.FileSystem
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$setup = Join-Path (Split-Path -Parent $here) 'Setup-Dependencies.ps1'
$pwshExe = (Get-Process -Id $PID).Path
$work = Join-Path ([System.IO.Path]::GetTempPath()) ("rs-setup-test-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $work | Out-Null
$script:fail = 0

function Assert-True($cond, [string]$msg) {
    if ($cond) { Write-Host "  PASS $msg" -ForegroundColor Green } else { Write-Host "  FAIL $msg" -ForegroundColor Red; $script:fail++ }
}

function New-Zip([string]$Path, [hashtable]$Files) {
    if (Test-Path $Path) { Remove-Item $Path -Force }
    $zip = [System.IO.Compression.ZipFile]::Open($Path, 'Create')
    try {
        foreach ($k in $Files.Keys) {
            $e = $zip.CreateEntry($k)
            $s = $e.Open(); $b = [System.Text.Encoding]::UTF8.GetBytes([string]$Files[$k]); $s.Write($b, 0, $b.Length); $s.Dispose()
        }
    } finally { $zip.Dispose() }
}

function Get-Sha([string]$p) { (Get-FileHash -Algorithm SHA256 -LiteralPath $p).Hash.ToLowerInvariant() }

function Sha-Of-Text([string]$t) {
    $sha = [System.Security.Cryptography.SHA256]::Create()
    ($sha.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($t)) | ForEach-Object { $_.ToString('x2') }) -join ''
}

function New-Lock([string]$Path, [hashtable]$Tools) {
    $lock = [ordered]@{ schema_version = 1; tools = $Tools }
    ($lock | ConvertTo-Json -Depth 8) | Set-Content -LiteralPath $Path -Encoding UTF8
}

function Tool-Entry([string]$Zip, [string]$Version, $Sha, [string]$EntrySha, [string]$Root = 'pkg') {
    @{
        version = $Version; install_dir = 'tooly'
        artifact = @{ name = (Split-Path -Leaf $Zip); url = [System.Uri]::new($Zip).AbsoluteUri; sha256 = $Sha; verify_required = ($null -eq $Sha) }
        layout = @{ archive_root = $Root; entry = 'bin/tool.exe'; entry_sha256 = $EntrySha }
    }
}

function Run-Setup([string]$Lock, [string]$Tools, [string[]]$More) {
    $env:REBUILD_STUDIO_DATA = Join-Path $work 'data'
    $args2 = @('-NoProfile', '-File', $setup, '-LockPath', $Lock, '-ToolsRoot', $Tools, '-AllowFileUrl') + $More
    if ($env:RS_TEST_DEBUG) { Write-Host ("ARGS: " + ($args2 -join " ")); Write-Host (Get-Content $Lock -Raw) }
    $out = & $pwshExe @args2 2>&1 | Out-String
    return [pscustomobject]@{ Code = $LASTEXITCODE; Out = $out }
}

try {
    $v1 = Join-Path $work 'v1.zip'; $v2 = Join-Path $work 'v2.zip'; $evil = Join-Path $work 'evil.zip'
    New-Zip $v1 @{ 'pkg/bin/tool.exe' = 'version-1'; 'pkg/readme.txt' = 'r1' }
    New-Zip $v2 @{ 'pkg/bin/tool.exe' = 'version-2' }
    New-Zip $evil @{ 'pkg/bin/tool.exe' = 'x'; 'pkg/../../escaped.txt' = 'pwned' }
    $sha1 = Get-Sha $v1; $sha2 = Get-Sha $v2; $shaEvil = Get-Sha $evil
    $tools = Join-Path $work 'tools'
    $lockPath = Join-Path $work 'lock.json'
    $entry = Join-Path $tools 'tooly/bin/tool.exe'

    Write-Host "1. dry run writes nothing"
    New-Lock $lockPath @{ tooly = (Tool-Entry $v1 '1.0.0' $sha1 (Sha-Of-Text 'version-1')) }
    $r = Run-Setup $lockPath $tools @('-DryRun')
    Assert-True ($r.Code -eq 0) "dry run exits 0 (got $($r.Code))"
    Assert-True (-not (Test-Path $tools)) 'dry run creates no tools dir'
    Assert-True ($r.Out -match 'would download') 'dry run explains the plan'

    Write-Host "2. install v1"
    $r = Run-Setup $lockPath $tools @()
    Assert-True ($r.Code -eq 0) "install exits 0 (got $($r.Code))"
    Assert-True ((Get-Content $entry -Raw) -eq 'version-1') 'v1 active'
    Assert-True (Test-Path (Join-Path $tools '.state/tooly.json')) 'state recorded'
    $r = Run-Setup $lockPath $tools @()
    Assert-True ($r.Out -match 'already active') 're-run is idempotent'

    Write-Host "3. upgrade to v2 keeps v1 as previous"
    New-Lock $lockPath @{ tooly = (Tool-Entry $v2 '2.0.0' $sha2 (Sha-Of-Text 'version-2')) }
    $r = Run-Setup $lockPath $tools @()
    Assert-True ($r.Code -eq 0) "upgrade exits 0 (got $($r.Code))"
    Assert-True ((Get-Content $entry -Raw) -eq 'version-2') 'v2 active'
    Assert-True (Test-Path (Join-Path $tools '.previous/tooly-1.0.0/bin/tool.exe')) 'v1 kept in .previous'

    Write-Host "4. rollback / roll forward"
    $r = Run-Setup $lockPath $tools @('-Rollback')
    Assert-True ($r.Code -eq 0) 'rollback exits 0'
    Assert-True ((Get-Content $entry -Raw) -eq 'version-1') 'v1 restored'
    $r = Run-Setup $lockPath $tools @('-Rollback')
    Assert-True ((Get-Content $entry -Raw) -eq 'version-2') 'second rollback returns to v2'

    Write-Host "5. refuse unknown hash"
    $tools2 = Join-Path $work 'tools2'
    New-Lock $lockPath @{ tooly = (Tool-Entry $v1 '1.0.0' $null $null) }
    $r = Run-Setup $lockPath $tools2 @()
    Assert-True ($r.Code -eq 3) "null hash refused with exit 3 (got $($r.Code))"
    Assert-True (-not (Test-Path (Join-Path $tools2 'tooly'))) 'nothing installed'

    Write-Host "6. wrong hash"
    New-Lock $lockPath @{ tooly = (Tool-Entry $v1 '1.0.0' ('0' * 64) $null) }
    $r = Run-Setup $lockPath $tools2 @()
    Assert-True ($r.Code -eq 1 -and $r.Out -match 'MISMATCH') "mismatch fails (exit $($r.Code))"
    Assert-True (-not (Test-Path (Join-Path $tools2 'tooly'))) 'nothing installed after mismatch'
    Assert-True (-not (Test-Path (Join-Path $tools2 '.staging/downloads/v1.zip'))) 'bad download deleted'

    Write-Host "7. entry binary hash mismatch"
    New-Lock $lockPath @{ tooly = (Tool-Entry $v1 '1.0.0' $sha1 ('f' * 64)) }
    $r = Run-Setup $lockPath $tools2 @()
    Assert-True ($r.Code -eq 1) "entry mismatch fails (exit $($r.Code))"
    Assert-True (-not (Test-Path (Join-Path $tools2 'tooly'))) 'nothing activated'

    Write-Host "8. zip-slip"
    New-Lock $lockPath @{ tooly = (Tool-Entry $evil '1.0.0' $shaEvil $null) }
    $r = Run-Setup $lockPath $tools2 @()
    Assert-True ($r.Code -eq 1) "zip-slip archive fails (exit $($r.Code))"
    Assert-True (-not (Test-Path (Join-Path $work 'escaped.txt')) -and -not (Test-Path (Join-Path $tools2 'escaped.txt'))) 'no file escaped'
    Assert-True (-not (Test-Path (Join-Path $tools2 'tooly'))) 'nothing activated'

    Write-Host "9. unknown tool name / file URL without -AllowFileUrl"
    $r = Run-Setup $lockPath $tools2 @('-Tool', 'nope')
    Assert-True ($r.Code -eq 2) "unknown tool exits 2 (got $($r.Code))"
    New-Lock $lockPath @{ tooly = (Tool-Entry $v1 '1.0.0' $sha1 $null) }
    $env:REBUILD_STUDIO_DATA = Join-Path $work 'data'
    $out = & $pwshExe -NoProfile -File $setup -LockPath $lockPath -ToolsRoot $tools2 2>&1 | Out-String
    Assert-True ($LASTEXITCODE -eq 3) "file:// URL refused without -AllowFileUrl (exit $LASTEXITCODE)"
} finally {
    Remove-Item -Recurse -Force -LiteralPath $work -ErrorAction SilentlyContinue
}

if ($script:fail -gt 0) { Write-Host "`n$($script:fail) check(s) FAILED" -ForegroundColor Red; exit 1 }
Write-Host "`nall Setup-Dependencies checks passed" -ForegroundColor Green
exit 0
