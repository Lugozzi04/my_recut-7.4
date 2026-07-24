param(
    [string]$Version = "1.0.0.0",
    [string]$IdentityName = "Lugozzi.AutoCutter",
    [string]$Publisher = "CN=EE1C02F9-02D8-415B-9FBF-AE705BF187BD",
    [string]$DisplayName = "Auto-Cutter",
    [string]$PublisherDisplayName = "Auto-Cutter",
    [switch]$SkipBuild,
    [string]$PfxPath = "",
    [string]$PfxPassword = ""
)

$ErrorActionPreference = "Stop"

function Resolve-ToolPath {
    param(
        [Parameter(Mandatory = $true)][string]$ToolName,
        [Parameter(Mandatory = $true)][string]$SdkExeName
    )
    $cmd = Get-Command $ToolName -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source) {
        return $cmd.Source
    }

    $root = "${env:ProgramFiles(x86)}\Windows Kits\10\bin"
    if (Test-Path $root) {
        $candidate = Get-ChildItem -Path $root -Recurse -Filter $SdkExeName -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -First 1
        if ($candidate) {
            return $candidate.FullName
        }
    }

    throw "$SdkExeName not found. Install Windows SDK and ensure $SdkExeName is available."
}

function Ensure-Dir {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$distDir = Join-Path $projectRoot "dist\AutoCutter"
$msixRoot = Join-Path $projectRoot "msix"
$staging = Join-Path $msixRoot "staging"
$assetsDir = Join-Path $staging "Assets"
$manifestTemplate = Join-Path $msixRoot "AppxManifest.template.xml"
$outputDir = Join-Path $msixRoot "output"
$msixPath = Join-Path $outputDir ("AutoCutter_" + $Version + ".msix")

if (-not $SkipBuild.IsPresent) {
    Write-Host "[1/5] Building app payload (PyInstaller)..." -ForegroundColor Cyan
    & powershell -ExecutionPolicy Bypass -File (Join-Path $projectRoot "build\build.ps1")
}

if (-not (Test-Path $distDir)) {
    throw "Missing dist folder: $distDir"
}
if (-not (Test-Path (Join-Path $distDir "AutoCutter.exe"))) {
    throw "Missing AutoCutter.exe in dist payload: $distDir"
}
if (-not (Test-Path $manifestTemplate)) {
    throw "Missing manifest template: $manifestTemplate"
}

Write-Host "[2/5] Preparing MSIX staging..." -ForegroundColor Cyan
if (Test-Path $staging) {
    Remove-Item -Recurse -Force $staging
}
Ensure-Dir -Path $staging
Ensure-Dir -Path $assetsDir
Ensure-Dir -Path $outputDir

Copy-Item -Path (Join-Path $distDir "*") -Destination $staging -Recurse -Force

$logoSource = Join-Path $projectRoot "icons\logo\logo.png"
if (-not (Test-Path $logoSource)) {
    throw "Missing logo source: $logoSource"
}

$assetTargets = @(
    "StoreLogo.png",
    "Square44x44Logo.png",
    "Square150x150Logo.png",
    "Square310x310Logo.png",
    "Wide310x150Logo.png",
    "SplashScreen.png"
)
foreach ($asset in $assetTargets) {
    Copy-Item -Path $logoSource -Destination (Join-Path $assetsDir $asset) -Force
}

Write-Host "[3/5] Rendering AppxManifest.xml..." -ForegroundColor Cyan
$manifest = Get-Content $manifestTemplate -Raw
$manifest = $manifest.Replace("__IDENTITY_NAME__", $IdentityName)
$manifest = $manifest.Replace("__PUBLISHER__", $Publisher)
$manifest = $manifest.Replace("__VERSION__", $Version)
$manifest = $manifest.Replace("__DISPLAY_NAME__", $DisplayName)
$manifest = $manifest.Replace("__PUBLISHER_DISPLAY_NAME__", $PublisherDisplayName)
Set-Content -Path (Join-Path $staging "AppxManifest.xml") -Value $manifest -Encoding UTF8

$makeAppx = Resolve-ToolPath -ToolName "makeappx.exe" -SdkExeName "makeappx.exe"
Write-Host "[4/5] Packing MSIX with makeappx..." -ForegroundColor Cyan
if (Test-Path $msixPath) {
    Remove-Item -Force $msixPath
}
& $makeAppx pack /d $staging /p $msixPath /o /nv
if ($LASTEXITCODE -ne 0) {
    throw "makeappx failed with exit code $LASTEXITCODE"
}

if ($PfxPath) {
    if (-not (Test-Path $PfxPath)) {
        throw "PFX not found: $PfxPath"
    }
    $signTool = Resolve-ToolPath -ToolName "signtool.exe" -SdkExeName "signtool.exe"
    Write-Host "[5/5] Signing MSIX..." -ForegroundColor Cyan
    & $signTool sign /fd SHA256 /f $PfxPath /p $PfxPassword /tr http://timestamp.digicert.com /td SHA256 $msixPath
    if ($LASTEXITCODE -ne 0) {
        throw "signtool failed with exit code $LASTEXITCODE"
    }
}
else {
    Write-Host "[5/5] Signing skipped (no -PfxPath provided)." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "MSIX ready: $msixPath" -ForegroundColor Green
