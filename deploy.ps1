#!/usr/bin/env pwsh
# Flowboard — one-command setup for a Windows host (parity with deploy.sh).
#
#   powershell -ExecutionPolicy Bypass -File .\deploy.ps1            # Postgres via Docker
#   powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -NoDocker  # SQLite, no Docker
#
# Idempotent: safe to re-run after `git pull`. On the first run it creates .env
# from the template (auto-filling a secret key) and stops so you can paste your
# Avis key + admin password; run it again to finish.
#
# Architecture: FastAPI + worker + SPA on :8101.
#   default   -> Postgres in Docker (:15432), schema via Alembic.
#   -NoDocker -> SQLite file under .\storage (schema auto-created on first boot).
# The backend is a single process, so SQLite (WAL) is safe for a studio-scale
# team. Switch to Postgres later if concurrent write load grows.

param([switch]$NoDocker)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $RepoRoot

function Say($m) { Write-Host "`n> $m" -ForegroundColor Cyan }
function Ok($m)  { Write-Host "OK $m" -ForegroundColor Green }
function Die($m) { Write-Host "`nX $m" -ForegroundColor Red; exit 1 }
function CheckExit($m) { if ($LASTEXITCODE -ne 0) { Die $m } }
function New-SecretKey { -join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Maximum 256) }) }

$SqliteUrl = "sqlite:///" + (($RepoRoot -replace '\\', '/') + "/storage/flowboard.db")
function Set-SqliteEnv {
  if (-not (Select-String -Path ".env" -Pattern '^FLOWBOARD_DATABASE_URL=' -Quiet)) {
    Add-Content ".env" "FLOWBOARD_DATABASE_URL=$SqliteUrl"
  }
}

# --- 0. tools ---------------------------------------------------------------
Say "Kiem tra cong cu"
function Need($cmd, $hint) {
  if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { Die "Thieu '$cmd' - cai: $hint" }
}
Need python "winget install Python.Python.3.12"
Need node   "winget install OpenJS.NodeJS.LTS"
Need npm    "winget install OpenJS.NodeJS.LTS"
if (-not $NoDocker) {
  Need docker "winget install Docker.DockerDesktop (mo app 1 lan) — hoac chay lai voi -NoDocker"
  docker info *> $null
  if ($LASTEXITCODE -ne 0) { Die "Docker engine chua chay - mo Docker Desktop, hoac chay lai voi -NoDocker (dung SQLite)." }
}
Ok ("Du cong cu" + $(if ($NoDocker) { " (che do -NoDocker / SQLite)" } else { "" }))

# --- 1. .env ----------------------------------------------------------------
if (-not (Test-Path ".env")) {
  Say "Chua co .env - tao tu mau + sinh SECRET_KEY"
  Copy-Item ".env.example" ".env"
  $key = New-SecretKey
  (Get-Content ".env") -replace '^FLOWBOARD_SECRET_KEY=.*', "FLOWBOARD_SECRET_KEY=$key" | Set-Content ".env"
  if ($NoDocker) { Set-SqliteEnv }
  Die "Da tao .env. Dien AVIS_API_KEY va FLOWBOARD_ADMIN_PASSWORD trong .env roi chay lai deploy.ps1"
}
if (-not (Select-String -Path ".env" -Pattern '^AVIS_API_KEY=.+' -Quiet))            { Die "Thieu AVIS_API_KEY trong .env" }
if (-not (Select-String -Path ".env" -Pattern '^FLOWBOARD_ADMIN_PASSWORD=.+' -Quiet)) { Die "Thieu FLOWBOARD_ADMIN_PASSWORD trong .env" }
if (-not (Select-String -Path ".env" -Pattern '^FLOWBOARD_SECRET_KEY=.+' -Quiet)) {
  $key = New-SecretKey
  (Get-Content ".env") -replace '^FLOWBOARD_SECRET_KEY=.*', "FLOWBOARD_SECRET_KEY=$key" | Set-Content ".env"
}
if ($NoDocker) { Set-SqliteEnv }
Ok ".env hop le"

# --- 2. backend -------------------------------------------------------------
Say "Backend: venv + deps"
Set-Location "$RepoRoot\agent"
if (-not (Test-Path ".venv")) { python -m venv .venv; CheckExit "Tao venv that bai" }
& ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip; CheckExit "Nang cap pip that bai"
& ".venv\Scripts\pip.exe" install --quiet -e .; CheckExit "Cai backend that bai"
Ok "Backend san sang"

# --- 3. database ------------------------------------------------------------
if ($NoDocker) {
  Say "Database: SQLite (khong Docker)"
  New-Item -ItemType Directory -Force -Path "$RepoRoot\storage" | Out-Null
  Ok "Se dung SQLite tai .\storage\flowboard.db (schema tu tao khi backend khoi dong lan dau)"
}
else {
  Say "Postgres (Docker) + migrate"
  docker compose up -d; CheckExit "docker compose up that bai"
  $healthy = $false
  for ($i = 0; $i -lt 30; $i++) {
    $s = (docker inspect -f '{{.State.Health.Status}}' flowboard-postgres 2>$null)
    if ($s -eq "healthy") { $healthy = $true; break }
    Start-Sleep -Seconds 1
  }
  if (-not $healthy) { Die "Postgres chua healthy sau 30s - kiem tra Docker Desktop." }
  & ".venv\Scripts\alembic.exe" upgrade head; CheckExit "alembic migrate that bai"
  Ok "DB da migrate"
}

# --- 4. frontend ------------------------------------------------------------
Say "Frontend: build (backend se tu serve)"
Set-Location "$RepoRoot\frontend"
npm ci; CheckExit "npm ci that bai"
npm run build; CheckExit "npm run build that bai"
Ok "Da build frontend/dist"

# --- done -------------------------------------------------------------------
Write-Host "`nSetup hoan tat." -ForegroundColor Green
Write-Host @"

> Chay thu (foreground):
    cd "$RepoRoot\agent"; .\.venv\Scripts\uvicorn.exe flowboard.main:app --host 127.0.0.1 --port 8101
  -> mo http://localhost:8101, dang nhap admin.

> Public HTTPS qua Cloudflare Tunnel (da login):
    cloudflared tunnel create giantstudio
    cloudflared tunnel route dns giantstudio giantstudio.reelmind.co
    # tao config.yml (xem DEPLOY-WINDOWS.md muc 6) roi:
    cloudflared tunnel run giantstudio

> Chay 24/7: xem DEPLOY-WINDOWS.md muc 7 (NSSM + cloudflared service).
"@ -ForegroundColor Gray
