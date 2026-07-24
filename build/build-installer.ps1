param(
    [string]$InnoCompiler = "C:\\Program Files (x86)\\Inno Setup 6\\ISCC.exe",
    [string]$AppVersion = "1.0.0",
    [switch]$BuildAppFirst,
    [string]$SignToolPath = "",
    [string]$PfxPath = "",
    [string]$PfxPassword = "",
    [string]$TimestampUrl = "http://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$DistDir = Join-Path $ProjectRoot "dist\\AutoCutter"
$IssPath = Join-Path $ProjectRoot "installer\\AutoCutter.iss"
$EnsureIconScript = Join-Path $ScriptDir "ensure-icon.ps1"

if ($BuildAppFirst) {
    & (Join-Path $ScriptDir "build.ps1")
}

if (Test-Path $EnsureIconScript) {
    Write-Host "Ensuring installer/app icon exists..." -ForegroundColor Cyan
    & $EnsureIconScript
}

if (-not (Test-Path $DistDir)) {
    throw "App build not found: $DistDir. Run build\\build.ps1 first."
}

if (-not (Test-Path $InnoCompiler)) {
    throw "Inno Setup compiler not found: $InnoCompiler"
}

if (-not (Test-Path $IssPath)) {
    throw "Installer script not found: $IssPath"
}

function Resolve-SignToolPath {
    param([string]$Candidate)
    if ($Candidate -and (Test-Path $Candidate)) {
        return (Resolve-Path $Candidate).Path
    }
    $fromCommand = Get-Command signtool -ErrorAction SilentlyContinue
    if ($null -ne $fromCommand) {
        return $fromCommand.Path
    }
    $kitsRoots = @(
        "${env:ProgramFiles(x86)}\\Windows Kits\\10\\bin",
        "${env:ProgramFiles}\\Windows Kits\\10\\bin"
    )
    foreach ($root in $kitsRoots) {
        if (-not (Test-Path $root)) { continue }
        $found = Get-ChildItem -Path $root -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($null -ne $found) {
            return $found.FullName
        }
    }
    return ""
}

function Sign-WithPowerShell {
    param(
        [string]$FilePath,
        [string]$CertificatePath,
        [string]$CertificatePassword,
        [string]$TimestampServer
    )
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2
    $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable
    $cert.Import($CertificatePath, $CertificatePassword, $flags)
    $result = Set-AuthenticodeSignature `
        -FilePath $FilePath `
        -Certificate $cert `
        -HashAlgorithm SHA256 `
        -TimestampServer $TimestampServer

    if ($null -eq $result.SignerCertificate) {
        throw "PowerShell signing failed: no signer certificate in result."
    }
    return $result
}

Write-Host "Compiling Inno Setup installer..." -ForegroundColor Cyan
Push-Location (Split-Path -Parent $IssPath)
try {
    & $InnoCompiler "/DMyAppVersion=$AppVersion" $IssPath
}
finally {
    Pop-Location
}

$OutputExe = Join-Path $ProjectRoot "installer\\output\\AutoCutterSetup.exe"
if (Test-Path $OutputExe) {
    Write-Host "Installer ready: $OutputExe" -ForegroundColor Green
} else {
    Write-Warning "Installer compile finished but output file was not found at expected path."
    exit 1
}

if ($PfxPath) {
    if (-not (Test-Path $PfxPath)) {
        throw "PFX certificate not found: $PfxPath"
    }
    $resolvedSignTool = Resolve-SignToolPath -Candidate $SignToolPath
    if (-not $resolvedSignTool) {
        throw "signtool.exe not found. Install Windows SDK or pass -SignToolPath."
    }
    if (-not $PfxPassword) {
        throw "Pfx password is required when -PfxPath is provided."
    }

    if ($resolvedSignTool) {
        Write-Host "Signing installer with signtool..." -ForegroundColor Cyan
        & $resolvedSignTool sign `
            /fd SHA256 `
            /td SHA256 `
            /tr $TimestampUrl `
            /f $PfxPath `
            /p $PfxPassword `
            $OutputExe

        Write-Host "Verifying signature..." -ForegroundColor Cyan
        & $resolvedSignTool verify /pa $OutputExe
        Write-Host "Signature applied successfully." -ForegroundColor Green
    } else {
        Write-Warning "signtool.exe not found. Using PowerShell Authenticode fallback."
        $psResult = Sign-WithPowerShell `
            -FilePath $OutputExe `
            -CertificatePath $PfxPath `
            -CertificatePassword $PfxPassword `
            -TimestampServer $TimestampUrl
        Write-Host ("PowerShell signature status: " + $psResult.Status) -ForegroundColor Green
    }
} else {
    Write-Host "No certificate provided: installer is unsigned." -ForegroundColor DarkYellow
}
