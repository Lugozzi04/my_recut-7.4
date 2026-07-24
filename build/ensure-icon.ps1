param(
    [string]$InputPng = ".\icons\logo\logo.png",
    [string]$OutputIco = ".\icons\logo\app.ico"
)

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = Resolve-Path (Join-Path $ScriptDir "..")

if (-not [System.IO.Path]::IsPathRooted($InputPng)) {
    $InputPng = Join-Path $ProjectRoot $InputPng
}
if (-not [System.IO.Path]::IsPathRooted($OutputIco)) {
    $OutputIco = Join-Path $ProjectRoot $OutputIco
}

if (-not (Test-Path $InputPng)) {
    throw "Logo PNG not found: $InputPng"
}

$outputDir = Split-Path -Parent $OutputIco
if (-not (Test-Path $outputDir)) {
    New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
}

Add-Type -AssemblyName System.Drawing
Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class IconTools {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool DestroyIcon(IntPtr hIcon);
}
"@

$source = [System.Drawing.Image]::FromFile($InputPng)
$size = 256
$bitmap = New-Object System.Drawing.Bitmap($size, $size, [System.Drawing.Imaging.PixelFormat]::Format32bppArgb)
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::HighQuality
$graphics.PixelOffsetMode = [System.Drawing.Drawing2D.PixelOffsetMode]::HighQuality
$graphics.Clear([System.Drawing.Color]::Transparent)
$graphics.DrawImage($source, 0, 0, $size, $size)

$hIcon = $bitmap.GetHicon()
$icon = [System.Drawing.Icon]::FromHandle($hIcon)

$stream = [System.IO.File]::Open($OutputIco, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write)
try {
    $icon.Save($stream)
}
finally {
    $stream.Dispose()
    $icon.Dispose()
    [IconTools]::DestroyIcon($hIcon) | Out-Null
    $graphics.Dispose()
    $bitmap.Dispose()
    $source.Dispose()
}

Write-Host "Icon generated: $OutputIco" -ForegroundColor Green
