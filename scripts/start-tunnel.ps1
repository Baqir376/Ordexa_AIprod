# Gives the Ordexa server on this PC a temporary public https address so Meta (WhatsApp) can reach it.
# Free Cloudflare "quick tunnel": no account needed. The address changes every time you run this, so paste the new
# one into Settings -> WhatsApp connection (and Meta's Callback URL) each time.
# Usage (PowerShell, from the ordexa folder, while the server is running):   .\scripts\start-tunnel.ps1
# Keep this window open while testing; closing it stops the tunnel.

$ErrorActionPreference = "Stop"

$cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
if (-not $cf) {
    $default = "C:\Program Files (x86)\cloudflared\cloudflared.exe"
    if (Test-Path $default) { $cf = $default }
}
if (-not $cf) {
    Write-Host "Installing Cloudflare Tunnel (cloudflared) with winget..." -ForegroundColor Cyan
    winget install --id Cloudflare.cloudflared -e --accept-source-agreements --accept-package-agreements
    $cf = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
    if (-not $cf -and (Test-Path "C:\Program Files (x86)\cloudflared\cloudflared.exe")) { $cf = "C:\Program Files (x86)\cloudflared\cloudflared.exe" }
    if (-not $cf) { throw "cloudflared was installed but not found. Close this window, open a new PowerShell and run the script again." }
}

try { Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 3 | Out-Null }
catch { Write-Host "Warning: the Ordexa server is not answering on port 8000. Run .\scripts\start-local.ps1 first." -ForegroundColor Yellow }

Write-Host ""
Write-Host "Starting tunnel. Look for a line like  https://something.trycloudflare.com" -ForegroundColor Green
Write-Host "Paste that address into Ordexa: Settings -> WhatsApp connection -> Public server address" -ForegroundColor Green
Write-Host ""
& $cf tunnel --url http://localhost:8000
