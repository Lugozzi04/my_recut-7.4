param(
    [string]$PythonExe = ".\\.venv310\\Scripts\\python.exe",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$SpecPath = Join-Path $ScriptDir "AutoCutter.spec"
$EnsureIconScript = Join-Path $ScriptDir "ensure-icon.ps1"

if (-not [System.IO.Path]::IsPathRooted($PythonExe)) {
    $PythonExe = Join-Path $ProjectRoot $PythonExe
}

if (-not (Test-Path $PythonExe)) {
    throw "Python executable not found: $PythonExe"
}

Write-Host "[1/3] Installing build dependencies..." -ForegroundColor Cyan
& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r (Join-Path $ProjectRoot "requirements-build.txt")

if (Test-Path $EnsureIconScript) {
    Write-Host "Generating app icon from logo..." -ForegroundColor Cyan
    & $EnsureIconScript
}

if ($Clean) {
    Write-Host "Cleaning previous build/dist folders..." -ForegroundColor DarkYellow
    Remove-Item -Recurse -Force (Join-Path $ProjectRoot "build\\work") -ErrorAction SilentlyContinue
    Remove-Item -Recurse -Force (Join-Path $ProjectRoot "dist\\AutoCutter") -ErrorAction SilentlyContinue
}

Write-Host "[2/3] Building AutoCutter with PyInstaller..." -ForegroundColor Cyan
Push-Location $ProjectRoot
try {
    & $PythonExe -m PyInstaller --noconfirm --clean `
        --workpath (Join-Path $ProjectRoot "build\\work") `
        --distpath (Join-Path $ProjectRoot "dist") `
        $SpecPath
}
finally {
    Pop-Location
}

$DistDir = Join-Path $ProjectRoot "dist\\AutoCutter"
if (-not (Test-Path $DistDir)) {
    throw "Build failed: dist folder not found ($DistDir)"
}

Write-Host "[3/3] Build complete." -ForegroundColor Green
Write-Host "App folder: $DistDir" -ForegroundColor Green
