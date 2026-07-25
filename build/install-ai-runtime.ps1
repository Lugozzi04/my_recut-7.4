param(
    [string]$PythonExe = "py",
    [string[]]$PythonArgs = @("-3.10"),
    [switch]$Recreate
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$RuntimeDir = Join-Path $ProjectRoot "ai_runtime"
$Requirements = Join-Path $ProjectRoot "requirements-ai.txt"
$RuntimePython = Join-Path $RuntimeDir "Scripts\python.exe"

function Invoke-NativeChecked {
    param([string]$Program, [string[]]$Arguments)
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code $LASTEXITCODE`: $Program $($Arguments -join ' ')"
    }
}

if (-not (Test-Path $Requirements)) {
    throw "AI requirements file not found: $Requirements"
}
if ($Recreate -and (Test-Path $RuntimeDir)) {
    Remove-Item -LiteralPath $RuntimeDir -Recurse -Force
}
if (-not (Test-Path $RuntimePython)) {
    Invoke-NativeChecked $PythonExe (@($PythonArgs) + @("-m", "venv", $RuntimeDir))
}

Invoke-NativeChecked $RuntimePython @("-m", "pip", "install", "--upgrade", "pip")
Invoke-NativeChecked $RuntimePython @("-m", "pip", "install", "--requirement", $Requirements)
Invoke-NativeChecked $RuntimePython @(
    "-c",
    "import silero_vad,spleeter,torch; print('AI runtime imports: ok')"
)

$manifest = [ordered]@{
    schema = 1
    python = (& $RuntimePython --version 2>&1 | Out-String).Trim()
    requirements_sha256 = (Get-FileHash -LiteralPath $Requirements -Algorithm SHA256).Hash.ToLowerInvariant()
    generated_at_utc = [DateTime]::UtcNow.ToString("o")
}
$manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $RuntimeDir "runtime-manifest.json") -Encoding UTF8
Write-Host "AI runtime ready: $RuntimeDir" -ForegroundColor Green
