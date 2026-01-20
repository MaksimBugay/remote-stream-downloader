# yt-dlp Streamer - Build and Deploy Script
# Usage: .\build-and-deploy.ps1

Write-Host "Stopping and removing existing container..." -ForegroundColor Yellow

# Stop and remove existing container (suppress errors if not exists)
docker stop yt-dlp-streamer 2>$null
docker rm yt-dlp-streamer 2>$null
docker image rm yt-dlp-streamer:latest 2>$null

Write-Host "Building new image..." -ForegroundColor Cyan
docker build -t yt-dlp-streamer:latest .

if ($LASTEXITCODE -ne 0) {
    Write-Host "Build failed!" -ForegroundColor Red
    exit 1
}

Write-Host "Starting container..." -ForegroundColor Green
docker compose up -d

Write-Host "Tailing logs (Ctrl+C to exit)..." -ForegroundColor Magenta
docker logs -f yt-dlp-streamer
