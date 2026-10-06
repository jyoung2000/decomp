<#
.SYNOPSIS
  Removes what Install-Clients.ps1 added (MCP server entry, skill, optional rules block). Other config is left as it was.
.PARAMETER Client   claude-code | codex | gemini | hermes | all
.PARAMETER DryRun   Show what would change.
#>
[CmdletBinding()]
param(
  [ValidateSet('claude-code','codex','gemini','hermes','all')][string]$Client = 'all',
  [switch]$DryRun,
  [string]$RuntimeDir
)
$ErrorActionPreference = 'Stop'
$installer = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'Install-Clients.ps1'
$p = @{ Client = $Client; Remove = $true }
if ($DryRun) { $p.DryRun = $true }
if ($RuntimeDir) { $p.RuntimeDir = $RuntimeDir }
& $installer @p
exit $LASTEXITCODE
