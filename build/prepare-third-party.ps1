param(
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")
$OutputDir = Join-Path $ScriptDir "third_party"
$Archive = Join-Path $OutputDir "ffmpeg-8.0.1.tar.xz"
$License = Join-Path $OutputDir "FFMPEG-GPL-3.0.txt"
$Notices = Join-Path $OutputDir "THIRD_PARTY_NOTICES.md"
$SourceUrl = "https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz"
$ExpectedSha256 = "05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41"

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null

$needsDownload = $Force -or (-not (Test-Path -LiteralPath $Archive -PathType Leaf))
if (-not $needsDownload) {
    $actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
    $needsDownload = $actual -ne $ExpectedSha256
}
if ($needsDownload) {
    Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
    Invoke-WebRequest -Uri $SourceUrl -OutFile $Archive
}

$actual = (Get-FileHash -LiteralPath $Archive -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $ExpectedSha256) {
    Remove-Item -LiteralPath $Archive -Force -ErrorAction SilentlyContinue
    throw "FFmpeg source checksum mismatch. Expected $ExpectedSha256, got $actual"
}

$licenseText = & tar -xOf $Archive "ffmpeg-8.0.1/COPYING.GPLv3"
if ($LASTEXITCODE -ne 0 -or -not $licenseText) {
    throw "Could not extract COPYING.GPLv3 from FFmpeg source archive."
}
[System.IO.File]::WriteAllLines($License, [string[]]$licenseText)
Copy-Item -LiteralPath (Join-Path $ProjectRoot "THIRD_PARTY_NOTICES.md") -Destination $Notices -Force

Write-Host "Third-party release assets ready: $OutputDir" -ForegroundColor Green
