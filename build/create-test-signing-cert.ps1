param(
    [string]$PfxPassword = "ChangeMe-StrongPassword-123!",
    [string]$OutputDir = ".\\cert"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
if (-not [System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir = Join-Path $ProjectRoot $OutputDir
}
New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

$subject = "CN=Auto-Cutter Test Code Signing"
$cert = New-SelfSignedCertificate `
    -Subject $subject `
    -Type CodeSigningCert `
    -CertStoreLocation "Cert:\\CurrentUser\\My" `
    -KeyExportPolicy Exportable `
    -HashAlgorithm "SHA256" `
    -NotAfter (Get-Date).AddYears(1)

$secure = ConvertTo-SecureString -String $PfxPassword -AsPlainText -Force
$pfxPath = Join-Path $OutputDir "AutoCutter-Test-CodeSigning.pfx"
Export-PfxCertificate -Cert $cert -FilePath $pfxPath -Password $secure | Out-Null

Write-Host "Test certificate created: $pfxPath" -ForegroundColor Green
Write-Host "IMPORTANT: This is for internal testing only; users will still see warnings." -ForegroundColor Yellow

