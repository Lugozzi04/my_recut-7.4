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

function Assert-WorkspaceTarget {
    param([string]$Target)
    $RootPath = [System.IO.Path]::GetFullPath([string]$ProjectRoot).TrimEnd('\', '/')
    $TargetPath = [System.IO.Path]::GetFullPath($Target)
    if (-not $TargetPath.StartsWith($RootPath + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Build cleanup target is outside the project: $TargetPath"
    }
    $CheckPath = $TargetPath
    while ($CheckPath -and $CheckPath -ne $RootPath) {
        if (Test-Path -LiteralPath $CheckPath) {
            $Item = Get-Item -LiteralPath $CheckPath -Force
            if ($Item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) {
                throw "Build cleanup refuses a symlink or junction: $CheckPath"
            }
        }
        $CheckPath = Split-Path -Parent $CheckPath
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

# Check even with -SkipDependencyInstall; do not erase an existing distribution
# and then silently build a release without its YouTube runtime.
Invoke-NativeChecked $PythonExe @((Join-Path $ScriptDir "check_dependencies.py"))

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
Assert-WorkspaceTarget $WorkDir
Assert-WorkspaceTarget $DistDir
foreach ($Target in @($WorkDir, $DistDir)) {
    if (Test-Path -LiteralPath $Target) {
        Remove-Item -LiteralPath $Target -Recurse -Force
    }
}
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
$ValidationRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("auto-cutter-build-smoke-" + [Guid]::NewGuid().ToString("N"))
$SmokeEnvironment = @{
    AUTO_CUTTER_CONFIG_DIR = (Join-Path $ValidationRoot "config")
    AUTO_CUTTER_DATA_DIR = (Join-Path $ValidationRoot "data")
    AUTO_CUTTER_PIPELINE_STORE = (Join-Path $ValidationRoot "data\pipeline\jobs.json")
    AUTO_CUTTER_CREDENTIALS_DIR = (Join-Path $ValidationRoot "credentials")
    AUTO_CUTTER_CACHE_DIR = (Join-Path $ValidationRoot "cache")
    AUTO_CUTTER_DOWNLOAD_DIR = (Join-Path $ValidationRoot "downloads")
    AUTO_CUTTER_OUTPUT_DIR = (Join-Path $ValidationRoot "outputs")
    QT_QPA_PLATFORM = "offscreen"
    QTWEBENGINE_CHROMIUM_FLAGS = "--disable-gpu"
}
$PreviousEnvironment = @{}
foreach ($Name in $SmokeEnvironment.Keys) {
    $PreviousEnvironment[$Name] = [Environment]::GetEnvironmentVariable($Name, "Process")
    [Environment]::SetEnvironmentVariable($Name, $SmokeEnvironment[$Name], "Process")
}
try {
    $SmokeProcess = Start-Process `
        -FilePath $AppExe `
        -ArgumentList "--smoke-test" `
        -WindowStyle Hidden `
        -Wait `
        -PassThru
    if ($SmokeProcess.ExitCode -ne 0) {
        throw "Packaged application smoke test failed with exit code $($SmokeProcess.ExitCode)."
    }
    Invoke-NativeChecked $PythonExe @((Join-Path $ScriptDir "verify_packaged_cli.py"), $AppExe)
}
finally {
    foreach ($Name in $SmokeEnvironment.Keys) {
        [Environment]::SetEnvironmentVariable($Name, $PreviousEnvironment[$Name], "Process")
    }
}

Write-Host "[4/4] Build complete." -ForegroundColor Green
Write-Host "App folder: $DistDir" -ForegroundColor Green
