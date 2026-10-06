<#
.SYNOPSIS
  Authenticode-sign one file with a PFX. Used by Build-RebuildStudio.ps1 -SignCert, and as Tauri's `bundle.windows.signCommand`
  (Tauri replaces %1 with the file to sign). Secrets are read from the environment, never from the command line:
    RS_SIGN_PFX        path to the .pfx
    RS_SIGN_PASSWORD   its password (may be empty)
    RS_SIGN_TIMESTAMP  RFC 3161 timestamp URL (default http://timestamp.digicert.com)
    RS_SIGN_ALLOW_UNTRUSTED=1   accept a signature whose chain is not trusted on this machine (self-signed test certificates ONLY)
  Exit 0 only when the file ends up carrying a signature that verifies (or, with ALLOW_UNTRUSTED, carries any signature).
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$Path)
$ErrorActionPreference = 'Stop'
$pfx = $env:RS_SIGN_PFX
if (-not $pfx -or -not (Test-Path -LiteralPath $pfx -PathType Leaf)) { Write-Error "RS_SIGN_PFX is not set or does not exist"; exit 2 }
$ts = $env:RS_SIGN_TIMESTAMP
if (-not $ts) { $ts = 'http://timestamp.digicert.com' }
$pw = New-Object System.Security.SecureString
foreach ($c in ([string]$env:RS_SIGN_PASSWORD).ToCharArray()) { $pw.AppendChar($c) }
$cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2($pfx, $pw, [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::DefaultKeySet)
if (-not $cert.HasPrivateKey) { Write-Error "certificate in $pfx has no private key"; exit 2 }
$sig = Set-AuthenticodeSignature -LiteralPath $Path -Certificate $cert -HashAlgorithm SHA256 -TimestampServer $ts
$ok = ($sig.Status -eq 'Valid')
if (-not $ok -and $env:RS_SIGN_ALLOW_UNTRUSTED -eq '1' -and $sig.SignerCertificate) { $ok = $true }
if (-not $ok) { Write-Error "signing $Path failed: $($sig.Status) $($sig.StatusMessage)"; exit 1 }
Write-Host "signed $Path ($($sig.Status)) by $($cert.Subject)"
exit 0
