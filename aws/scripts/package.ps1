$ErrorActionPreference = "Stop"

Write-Host "=== TruthPhone packaging ===" -ForegroundColor Cyan

python scripts/build_lambda.py --target all

Push-Location frontend
try {
    if (-not (Test-Path "node_modules")) {
        npm install --no-audit --no-fund
    }
    npm run build
}
finally {
    Pop-Location
}

Write-Host ""
Write-Host "Artifacts:" -ForegroundColor Green
Write-Host "  aws/dist/api.zip"
Write-Host "  aws/dist/worker.zip"
Write-Host "  aws/frontend/dist/"
Write-Host ""
Write-Host "Next: read aws/DEPLOY.md and deploy through the AWS console."
