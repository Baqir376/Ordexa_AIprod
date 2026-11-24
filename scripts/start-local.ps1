# Starts Ordexa on this PC for testing: database, API, WhatsApp webhook and workers (Docker), then the desktop app.
# Usage (PowerShell, from the ordexa folder):   .\scripts\start-local.ps1
# Secrets (GROQ_API_KEY etc.) are read from the .env file next to docker-compose.yml.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 1. Docker Desktop
docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Starting Docker Desktop..." -ForegroundColor Cyan
    Start-Process "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 3
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { break }
    }
    if ($LASTEXITCODE -ne 0) { throw "Docker did not start. Open Docker Desktop and run this script again." }
}

# 2. Database, migrations, API, webhook, workers
Write-Host "Starting Ordexa server (this can take a minute the first time)..." -ForegroundColor Cyan
docker compose up -d --build
for ($i = 0; $i -lt 40; $i++) {
    try {
        $h = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 3
        if ($h.status -eq "ok") { break }
    } catch { Start-Sleep -Seconds 2 }
}
Write-Host "Server is running at http://127.0.0.1:8000" -ForegroundColor Green

# 3. Desktop app
Write-Host "Opening the Ordexa desktop app..." -ForegroundColor Cyan
Set-Location (Join-Path $root "desktop")
if (-not (Test-Path "node_modules")) { npm install }
npm run dev
