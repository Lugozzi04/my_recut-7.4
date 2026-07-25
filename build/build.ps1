param(
    [string]$PythonExe = ".\\.venv310\\Scripts\\python.exe",
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$SpecPath = Join-Path $ScriptDir "AutoCutter.spec"
$EnsureIconScript = Join-Path $ScriptDir "ensure-icon.ps1"
$PrepareThirdPartyScript = Join-Path $ScriptDir "prepare-third-party.ps1"

function Invoke-NativeChecked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $Program $($Arguments -join ' ')"
    }
}

if (-not [System.IO.Path]::IsPathRooted($PythonExe)) {
    $PythonExe = Join-Path $ProjectRoot $PythonExe
}

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}

if (-not $SkipDependencyInstall) {
    Write-Host "[1/4] Installing locked build dependencies..." -ForegroundColor Cyan
    Invoke-NativeChecked $PythonExe @(
        "-m", "pip", "install",
        "--requirement", (Join-Path $ProjectRoot "requirements-build.txt")
    )
} else {
    Write-Host "[1/4] Dependency installation skipped." -ForegroundColor DarkYellow
}

if (Test-Path $EnsureIconScript) {
    Write-Host "Generating app icon from logo..." -ForegroundColor Cyan
    & $EnsureIconScript
}
if (-not (Test-Path -LiteralPath $PrepareThirdPartyScript -PathType Leaf)) {
    throw "Third-party preparation script not found: $PrepareThirdPartyScript"
}
& $PrepareThirdPartyScript

$WorkDir = Join-Path $ProjectRoot "build\\work"
$DistDir = Join-Path $ProjectRoot "dist\\AutoCutter"
Write-Host "Removing previous AutoCutter build targets..." -ForegroundColor DarkYellow
Remove-Item -LiteralPath $WorkDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $DistDir -Recurse -Force -ErrorAction SilentlyContinue
$BuildStartedUtc = [DateTime]::UtcNow

Write-Host "[2/4] Building AutoCutter with PyInstaller..." -ForegroundColor Cyan
Push-Location $ProjectRoot
try {
    Invoke-NativeChecked $PythonExe @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--workpath", $WorkDir,
        "--distpath", (Join-Path $ProjectRoot "dist"),
        $SpecPath
    )
}
finally {
    Pop-Location
}

$AppExe = Join-Path $DistDir "AutoCutter.exe"
if (-not (Test-Path -LiteralPath $AppExe -PathType Leaf)) {
    throw "Build failed: application executable not found ($AppExe)"
}
$BuiltExe = Get-Item -LiteralPath $AppExe
if ($BuiltExe.LastWriteTimeUtc -lt $BuildStartedUtc.AddSeconds(-2)) {
    throw "Build failed: application executable was not produced by the current build."
}

Write-Host "[3/4] Running packaged application smoke test..." -ForegroundColor Cyan
$SmokeProcess = Start-Process `
    -FilePath $AppExe `
    -ArgumentList "--smoke-test" `
    -WindowStyle Hidden `
    -Wait `
    -PassThru
if ($SmokeProcess.ExitCode -ne 0) {
    throw "Packaged application smoke test failed with exit code $($SmokeProcess.ExitCode)."
}

Write-Host "[4/4] Build complete." -ForegroundColor Green
Write-Host "App folder: $DistDir" -ForegroundColor Green
