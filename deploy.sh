#!/usr/bin/env bash
# Flowboard — one-command setup for a Mac Mini (or any macOS host).
#
#   ./deploy.sh
#
# Idempotent: safe to re-run after `git pull`. On the first run it creates .env
# from the template (auto-filling a secret key) and stops so you can paste your
# Avis key + admin password; run it again to finish.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

say()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m✓ %s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31m✖ %s\033[0m\n' "$*" >&2; exit 1; }

# ── 0. tools ────────────────────────────────────────────────────────────────
say "Kiểm tra công cụ"
need() { command -v "$1" >/dev/null 2>&1 || die "Thiếu '$1' — cài: $2"; }
need python3 "brew install python@3.12"
need node    "brew install node"
need npm     "brew install node"
need docker  "cài OrbStack: brew install orbstack (rồi mở app 1 lần)"
need openssl "có sẵn trên macOS"
docker info >/dev/null 2>&1 || die "Docker engine chưa chạy — mở OrbStack/Docker Desktop rồi chạy lại."
ok "Đủ công cụ"

# ── 1. .env ─────────────────────────────────────────────────────────────────
if [[ ! -f .env ]]; then
  say "Chưa có .env — tạo từ mẫu + sinh SECRET_KEY"
  cp .env.example .env
  key="$(openssl rand -hex 32)"
  sed -i '' "s|^FLOWBOARD_SECRET_KEY=.*|FLOWBOARD_SECRET_KEY=${key}|" .env
  die "Đã tạo .env. Hãy điền AVIS_API_KEY và FLOWBOARD_ADMIN_PASSWORD trong .env rồi chạy lại ./deploy.sh"
fi
grep -q '^AVIS_API_KEY=.\+'             .env || die "Thiếu AVIS_API_KEY trong .env"
grep -q '^FLOWBOARD_ADMIN_PASSWORD=.\+' .env || die "Thiếu FLOWBOARD_ADMIN_PASSWORD trong .env"
if ! grep -q '^FLOWBOARD_SECRET_KEY=.\+' .env; then
  key="$(openssl rand -hex 32)"
  sed -i '' "s|^FLOWBOARD_SECRET_KEY=.*|FLOWBOARD_SECRET_KEY=${key}|" .env
fi
ok ".env hợp lệ"

# ── 2. backend ──────────────────────────────────────────────────────────────
say "Backend: venv + deps"
cd "$REPO_ROOT/agent"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e .
ok "Backend sẵn sàng"

# ── 3. database ─────────────────────────────────────────────────────────────
say "Postgres (Docker) + migrate"
docker compose up -d
for _ in $(seq 1 30); do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' flowboard-postgres 2>/dev/null)" == "healthy" ]] && break
  sleep 1
done
.venv/bin/alembic upgrade head
ok "DB đã migrate"

# ── 4. frontend ─────────────────────────────────────────────────────────────
say "Frontend: build (backend sẽ tự serve)"
cd "$REPO_ROOT/frontend"
npm ci
npm run build
ok "Đã build frontend/dist"

# ── done ────────────────────────────────────────────────────────────────────
cat <<EOF

$(printf '\033[1;32m✅ Setup hoàn tất.\033[0m')

▶ Chạy thử (foreground):
    cd "$REPO_ROOT/agent" && .venv/bin/uvicorn flowboard.main:app --host 0.0.0.0 --port 8101
  → mở http://localhost:8101, đăng nhập admin.

▶ Chạy 24/7 (tự bật lại khi reboot):
    bash "$REPO_ROOT/packaging/install-launchd.sh"

▶ Public HTTPS (tạm — đưa URL cho user):
    cloudflared tunnel --url http://localhost:8101
EOF
