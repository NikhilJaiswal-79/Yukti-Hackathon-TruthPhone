$ErrorActionPreference = "Stop"

# Prefer the project venv so strands-agents / bedrock-agentcore are importable
# even when the shell has not been activated.
$python = Join-Path (Split-Path -Parent $PSScriptRoot) ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    $python = "python"
    Write-Host "No aws\.venv found; using the python on PATH." -ForegroundColor Yellow
}

# LOCAL_WORKER runs the worker in this process instead of invoking the deployed
# Lambda. Everything else comes from aws\.env (see backend/truthphone/config.py).
$env:LOCAL_WORKER = "1"

$envFile = Join-Path (Split-Path -Parent $PSScriptRoot) ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "No aws\.env found. Copy aws\.env.example to aws\.env and set SESSION_TABLE." -ForegroundColor Yellow
}

Write-Host "Local API: http://localhost:8000" -ForegroundColor Cyan
Write-Host "Local UI:  http://localhost:5173" -ForegroundColor Cyan
Write-Host "Worker runs in-process (LOCAL_WORKER=1)." -ForegroundColor Yellow

Push-Location backend
try {
    & $python -m uvicorn truthphone.api:app --reload --port 8000
}
finally {
    Pop-Location
}
