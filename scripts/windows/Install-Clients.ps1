<#
.SYNOPSIS
  Installs the Rebuild Studio client packages (MCP server entry + skill) for Claude Code, Codex, Gemini CLI and/or Hermes.
.DESCRIPTION
  Thin wrapper around scripts\install-clients.py that uses the bundled Python runtime and bundled rebuild-mcp.exe.
  Nothing is required for the desktop app itself; this is optional and only for command line agents.
  Use -DryRun first: it prints a diff and changes nothing. Existing config is merged (never overwritten) and backed up.
.PARAMETER Client      claude-code | codex | gemini | hermes | all   (all = only the clients detected on this machine)
.PARAMETER DryRun      Show what would change.
.PARAMETER RuntimeDir  Bundled runtime folder (contains python.exe and Scripts\rebuild-mcp.exe). Auto-detected next to the install root.
.PARAMETER McpCommand  Explicit path to rebuild-mcp.exe (overrides detection).
.PARAMETER DataDir     Data directory to pass to the MCP server (always written for Hermes).
.PARAMETER Toolset     minimal | analysis | rebuild | all (default all)
.PARAMETER Rules       Also add the short rules block to the global AGENTS.md / GEMINI.md (Codex, Gemini).
#>
[CmdletBinding()]
param(
  [ValidateSet('claude-code','codex','gemini','hermes','all')][string]$Client = 'all',
  [switch]$DryRun,
  [string]$RuntimeDir,
  [string]$McpCommand,
  [string]$DataDir,
  [ValidateSet('minimal','analysis','rebuild','all')][string]$Toolset = 'all',
  [switch]$Rules,
  [switch]$Remove
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$scriptsDir = Split-Path -Parent $here                       # ...\scripts
$py = Join-Path $scriptsDir 'install-clients.py'
if (-not (Test-Path $py)) { throw "install-clients.py not found next to this script ($py)" }

# Locate the bundled runtime: -RuntimeDir, else a 'runtime' folder in one of the parent directories.
if (-not $RuntimeDir) {
  $dir = $scriptsDir
  for ($i = 0; $i -lt 4 -and $dir; $i++) {
    $cand = Join-Path $dir 'runtime'
    if (Test-Path $cand) { $RuntimeDir = $cand; break }
    $dir = Split-Path -Parent $dir
  }
}
$python = $null
if ($RuntimeDir) {
  foreach ($c in @((Join-Path $RuntimeDir 'python.exe'), (Join-Path $RuntimeDir 'python\python.exe'), (Join-Path $RuntimeDir 'Scripts\python.exe'))) {
    if (Test-Path $c) { $python = $c; break }
  }
  if (-not $McpCommand) {
    foreach ($c in @((Join-Path $RuntimeDir 'Scripts\rebuild-mcp.exe'), (Join-Path $RuntimeDir 'rebuild-mcp.exe'))) {
      if (Test-Path $c) { $McpCommand = $c; break }
    }
  }
}
if (-not $python) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  if (-not $cmd) { throw "No bundled runtime found (use -RuntimeDir) and no python on PATH." }
  $python = $cmd.Source
  Write-Warning "Bundled runtime not found; using $python"
}

$argsList = @($py, '--client', $Client, '--toolset', $Toolset)
if ($McpCommand) { $argsList += @('--mcp-command', $McpCommand) }
if ($DataDir)    { $argsList += @('--data-dir', $DataDir) }
if ($DryRun)     { $argsList += '--dry-run' }
if ($Rules)      { $argsList += '--rules' }
if ($Remove)     { $argsList += '--remove' }
$clientsDir = Join-Path (Split-Path -Parent $scriptsDir) 'clients'
if (Test-Path $clientsDir) { $argsList += @('--clients-dir', $clientsDir) }

& $python @argsList
exit $LASTEXITCODE
