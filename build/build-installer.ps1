param(
    [string]$InnoCompiler = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    [string]$AppVersion = "",
    [switch]$BuildAppFirst,
    [string]$SignToolPath = "",
    [string]$PfxPath = "",
    [string]$PfxPassword = "",
    [string]$TimestampUrl = "https://timestamp.digicert.com"
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$DistDir = Join-Path $ProjectRoot "dist\AutoCutter"
$AppExe = Join-Path $DistDir "AutoCutter.exe"
$IssPath = Join-Path $ProjectRoot "installer\AutoCutter.iss"
$OutputExe = Join-Path $ProjectRoot "installer\output\AutoCutterSetup.exe"
$EnsureIconScript = Join-Path $ScriptDir "ensure-icon.ps1"
$VersionPath = Join-Path $ProjectRoot "VERSION"

function Invoke-NativeChecked {
    param(
        [string]$Program,
        [string[]]$Arguments,
        [string]$FailureMessage = "Native command failed"
    )
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$FailureMessage (exit code $LASTEXITCODE)"
    }
}

function Resolve-SignToolPath {
    param([string]$Candidate)
    if ($Candidate -and (Test-Path -LiteralPath $Candidate -PathType Leaf)) {
        return (Resolve-Path $Candidate).Path
    }
    $fromCommand = Get-Command signtool -ErrorAction SilentlyContinue
    if ($null -ne $fromCommand) {
        return $fromCommand.Path
    }
    $kitsRoots = @(
        "${env:ProgramFiles(x86)}\Windows Kits\10\bin",
        "${env:ProgramFiles}\Windows Kits\10\bin"
    )
    foreach ($root in $kitsRoots) {
        if (-not (Test-Path -LiteralPath $root)) { continue }
        $found = Get-ChildItem -LiteralPath $root -Recurse -Filter signtool.exe -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($null -ne $found) {
            return $found.FullName
        }
    }
    return ""
}

function Sign-Artifact {
    param(
        [string]$FilePath,
        [string]$ResolvedSignTool,
        [string]$CertificatePath,
        [string]$CertificatePassword,
        [string]$TimestampServer
    )
    if ($ResolvedSignTool) {
        Invoke-NativeChecked $ResolvedSignTool @(
            "sign", "/fd", "SHA256", "/td", "SHA256", "/tr", $TimestampServer,
            "/f", $CertificatePath, "/p", $CertificatePassword, $FilePath
        ) "Code signing failed"
        Invoke-NativeChecked $ResolvedSignTool @("verify", "/pa", $FilePath) "Signature verification failed"
        return
    }

    Write-Warning "signtool.exe not found. Using PowerShell Authenticode fallback."
    $cert = New-Object System.Security.Cryptography.X509Certificates.X509Certificate2
    $flags = [System.Security.Cryptography.X509Certificates.X509KeyStorageFlags]::Exportable
    $cert.Import($CertificatePath, $CertificatePassword, $flags)
    $result = Set-AuthenticodeSignature `
        -FilePath $FilePath `
        -Certificate $cert `
        -HashAlgorithm SHA256 `
        -TimestampServer $TimestampServer
    if ($null -eq $result.SignerCertificate -or $result.Status -ne "Valid") {
        throw "PowerShell signature verification failed: $($result.Status) $($result.StatusMessage)"
    }
}

if (-not $AppVersion) {
    if (-not (Test-Path -LiteralPath $VersionPath -PathType Leaf)) {
        throw "VERSION file not found: $VersionPath"
    }
    $AppVersion = (Get-Content -LiteralPath $VersionPath -Raw).Trim()
}
if ($AppVersion -notmatch '^\d+\.\d+\.\d+([.-][0-9A-Za-z.-]+)?$') {
    throw "Invalid application version: $AppVersion"
}

if ($BuildAppFirst) {
    & (Join-Path $ScriptDir "build.ps1")
}
if (Test-Path -LiteralPath $EnsureIconScript -PathType Leaf) {
    & $EnsureIconScript
}
if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) {
    throw "App build not found: $AppExe. Run build\build.ps1 first."
}
if (-not (Test-Path -LiteralPath $InnoCompiler -PathType Leaf)) {
    throw "Inno Setup compiler not found: $InnoCompiler"
}
if (-not (Test-Path -LiteralPath $IssPath -PathType Leaf)) {
    throw "Installer script not found: $IssPath"
}

$resolvedSignTool = ""
if ($PfxPath) {
    if (-not (Test-Path -LiteralPath $PfxPath -PathType Leaf)) {
        throw "PFX certificate not found: $PfxPath"
    }
    if (-not $PfxPassword) {
        throw "Pfx password is required when -PfxPath is provided."
    }
    $resolvedSignTool = Resolve-SignToolPath -Candidate $SignToolPath
    Write-Host "Signing application executable..." -ForegroundColor Cyan
    Sign-Artifact $AppExe $resolvedSignTool $PfxPath $PfxPassword $TimestampUrl
} else {
    Write-Warning "No certificate provided: application and installer will be unsigned."
}

Remove-Item -LiteralPath $OutputExe -Force -ErrorAction SilentlyContinue
$CompileStartedUtc = [DateTime]::UtcNow
Write-Host "Compiling Inno Setup installer v$AppVersion..." -ForegroundColor Cyan
Push-Location (Split-Path -Parent $IssPath)
try {
    Invoke-NativeChecked $InnoCompiler @("/DMyAppVersion=$AppVersion", $IssPath) "Installer compilation failed"
}
finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath $OutputExe -PathType Leaf)) {
    throw "Installer compilation finished without producing: $OutputExe"
}
if ((Get-Item -LiteralPath $OutputExe).LastWriteTimeUtc -lt $CompileStartedUtc.AddSeconds(-2)) {
    throw "Installer output was not produced by the current build."
}

if ($PfxPath) {
    Write-Host "Signing installer..." -ForegroundColor Cyan
    Sign-Artifact $OutputExe $resolvedSignTool $PfxPath $PfxPassword $TimestampUrl
}

Write-Host "Installer ready: $OutputExe" -ForegroundColor Green
