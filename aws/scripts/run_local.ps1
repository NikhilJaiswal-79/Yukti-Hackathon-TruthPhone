$ErrorActionPreference = "Stop"

$env:LOCAL_WORKER = "1"
$env:SESSION_TABLE = if ($env:SESSION_TABLE) { $env:SESSION_TABLE } else { "truthphone-sessions" }
$env:BEDROCK_MODEL_ID = if ($env:BEDROCK_MODEL_ID) { $env:BEDROCK_MODEL_ID } else { "in.anthropic.claude-haiku-4-5-20251001-v1:0" }
$env:AWS_REGION = if ($env:AWS_REGION) { $env:AWS_REGION } else { "ap-south-1" }
$env:ALLOWED_ORIGINS = if ($env:ALLOWED_ORIGINS) { $env:ALLOWED_ORIGINS } else { "*" }

Write-Host "Local API: http://localhost:8000" -ForegroundColor Cyan
Write-Host "Local UI:  http://localhost:5173" -ForegroundColor Cyan
Write-Host "Worker runs in-process (LOCAL_WORKER=1)." -ForegroundColor Yellow

Push-Location backend
try {
    python -m uvicorn truthphone.api:app --reload --port 8000
}
finally {
    Pop-Location
}
