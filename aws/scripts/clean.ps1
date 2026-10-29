$ErrorActionPreference = "Stop"

Write-Host "=== TruthPhone cleanup ===" -ForegroundColor Cyan

$awsDir = Split-Path -Parent $PSScriptRoot

# Rebuilt by scripts/package.ps1 (build_lambda.py + npm).
$targets = @(
    (Join-Path $awsDir "build")
    (Join-Path $awsDir "dist")
    (Join-Path $awsDir "frontend\dist")
    (Join-Path $awsDir "frontend\node_modules")
    (Join-Path $awsDir "frontend\public\nice-dcv-web-client-sdk")
)

foreach ($path in $targets) {
    if (Test-Path $path) {
        Write-Host "Removing $path"
        Remove-Item -Recurse -Force $path
    }
}

$caches = Get-ChildItem -Path $awsDir -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue
foreach ($cache in $caches) {
    Write-Host "Removing $($cache.FullName)"
    Remove-Item -Recurse -Force $cache.FullName -ErrorAction SilentlyContinue
}

Write-Host ""
Write-Host "Removed build outputs, node_modules and __pycache__." -ForegroundColor Green
Write-Host "Nothing here is needed by the deployed stack (Lambda and S3 keep their own copies)."
Write-Host "Rebuild with: .\scripts\package.ps1"
