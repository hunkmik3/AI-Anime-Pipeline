# Deploy Flowboard (multi-user) on Windows

Same architecture as the Mac Mini guide ([DEPLOY.md](DEPLOY.md)): one backend
process serves **both the API and the web UI** on port `8101` (it serves
`frontend/dist`). A Cloudflare Tunnel gives the public HTTPS URL
**https://giantstudio.reelmind.co**. The Avis key stays server-side; users log
in with admin-provisioned accounts.

```
Browser ─HTTPS→ Cloudflare Tunnel (giantstudio.reelmind.co) ─→ localhost:8101
                                                      (FastAPI + worker + SPA)
                                                        └─ database (see below)
                                                        └─ Avis API (Seedance video)
```

### Database: Docker or no-Docker
Pick one — the rest of the guide is identical:

| | **Postgres via Docker** (default) | **SQLite, no Docker** (`-NoDocker`) |
|---|---|---|
| Extra install | Docker Desktop (needs WSL2 + BIOS virtualization) | none |
| Data | Docker volume `flowboard_pgdata` | `.\storage\flowboard.db` |
| Best for | heavier concurrent load / future scale | simplest; fine for a studio team (backend is one process, SQLite runs in WAL mode) |

The backend is a **single process** (API + worker in one), so SQLite is safe
here — the "no-Docker" path just runs `deploy.ps1 -NoDocker`, which sets
`FLOWBOARD_DATABASE_URL=sqlite:///…/storage/flowboard.db` and skips Docker,
Postgres, and Alembic entirely (the schema is created automatically on first
boot). You can move to Postgres later without code changes — just change that
one env var. **If you don't need Docker, take the SQLite path — it's the least
to install on a bare Windows machine.**

> This deploys the **committed stable version**: Seedance video gen + multi-user
> + budgets + admin panel. It does **not** include the experimental Gemini /
> master-shot / i2v image pipeline (that stays on the dev Mac, uncommitted).

Run every command below in **PowerShell** (open as Administrator for the tool
installs and the service steps).

## 1. Install tools (once)
Windows 10/11 ship `winget`. Install as Administrator:
```powershell
winget install --id Git.Git -e
winget install --id OpenJS.NodeJS.LTS -e        # Node 20
winget install --id Python.Python.3.12 -e
winget install --id Cloudflare.cloudflared -e
# Docker ONLY if you want the Postgres path (skip it for the SQLite / -NoDocker path):
winget install --id Docker.DockerDesktop -e     # needs WSL2 + virtualization
```
Then:
- **Reopen PowerShell** so the new PATH entries load.
- **(Docker path only)** Launch Docker Desktop once and wait until it says
  *Engine running*. If it asks to install/enable WSL2, accept and reboot — Docker
  needs the WSL2 engine, and virtualization must be enabled in BIOS. **If this is
  a hassle, use the SQLite path instead and skip Docker entirely.**
- Verify: `git --version; node -v; python --version; cloudflared --version`
  (add `docker info` on the Docker path).

## 2. Clone
```powershell
git clone https://github.com/hunkmik3/AI-Anime-Pipeline.git C:\flowboard
cd C:\flowboard
git checkout anime-adaptation
```

## 3. Configure secrets
```powershell
copy .env.example .env
notepad .env
```
Fill:
- `AVIS_API_KEY=` — the shared Avis key (server-side, powers video for all users).
- `FLOWBOARD_ADMIN_PASSWORD=` — a strong admin password (NOT `admin12345`).
- `FLOWBOARD_SECRET_KEY=` — leave blank; `deploy.ps1` generates it.
- `R2_*` — only if you want KYC (person-driven) video; normal Seedance video
  works without R2. See [docs/r2_setup.md](docs/r2_setup.md).

`FLOWBOARD_REQUIRE_AUTH=1`, `FLOWBOARD_DISABLE_BRIDGE=1`, and
`FLOWBOARD_DEFAULT_VIDEO_MODEL=seedance-2-0` are already set in the template.

## 4. One-command setup
```powershell
# SQLite, no Docker (simplest — recommended if you don't already run Docker):
powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -NoDocker

# ── or ── Postgres via Docker (Docker Desktop must be running):
powershell -ExecutionPolicy Bypass -File .\deploy.ps1
```
It checks tools, creates the venv + installs the backend, sets up the database
(Docker+Postgres+Alembic, or SQLite auto-schema with `-NoDocker`), and builds the
frontend. Re-runnable after every `git pull`. (First run, if `.env` was missing,
it creates one and stops — fill it, then run again **with the same flag**.)

## 5. Run (test)
```powershell
cd C:\flowboard\agent
.\.venv\Scripts\uvicorn.exe flowboard.main:app --host 127.0.0.1 --port 8101
```
Open <http://localhost:8101>, log in as `admin`. Ctrl+C to stop. (127.0.0.1 is
enough — only the tunnel, running on this machine, needs to reach it.)

## 6. Public HTTPS — Cloudflare Tunnel → giantstudio.reelmind.co
You are already logged in (`cloudflared tunnel login` done). `reelmind.co` must
be a zone in that Cloudflare account.

```powershell
cloudflared tunnel create giantstudio
# ^ prints a tunnel UUID and writes creds to C:\Users\<you>\.cloudflared\<UUID>.json
cloudflared tunnel route dns giantstudio giantstudio.reelmind.co
```
Create `C:\Users\<you>\.cloudflared\config.yml`:
```yaml
tunnel: <UUID-from-create>
credentials-file: C:\Users\<you>\.cloudflared\<UUID>.json
ingress:
  - hostname: giantstudio.reelmind.co
    service: http://localhost:8101
  - service: http_status:404
```
Test (with the backend from step 5 running in another window):
```powershell
cloudflared tunnel run giantstudio
```
Open <https://giantstudio.reelmind.co> — you should reach the login page.

## 7. Run 24/7 (Windows services — auto-start on boot)
Install NSSM, then register the backend and the tunnel as services.
```powershell
winget install --id NSSM.NSSM -e     # or download from https://nssm.cc

# Backend service
nssm install Flowboard "C:\flowboard\agent\.venv\Scripts\uvicorn.exe" "flowboard.main:app --host 127.0.0.1 --port 8101"
nssm set Flowboard AppDirectory "C:\flowboard\agent"
nssm set Flowboard AppStdout "C:\flowboard\logs\backend.log"
nssm set Flowboard AppStderr "C:\flowboard\logs\backend.log"
nssm start Flowboard
```
Tunnel service (native installer reads the `config.yml` from step 6):
```powershell
cloudflared service install
# manage: sc stop cloudflared / sc start cloudflared
```
> Docker Desktop must be set to **start on login** (Settings → General →
> *Start Docker Desktop when you sign in*) so Postgres is up before the backend
> service starts. If the backend races Docker on boot, NSSM auto-restarts it
> until Postgres is healthy.

Remove later: `nssm remove Flowboard confirm` ; `cloudflared service uninstall`.

## 8. After it's up
1. Log in as `admin` → **change the password**.
2. **Quản lý tài khoản** → create users, set each user's **$ budget**.
3. Per user: **Hoạt động** (what they generated, cost, output), **Xoá** to remove.

## Operating notes
- ⚠️ **Sum of all user budgets ≤ real Avis balance** — one shared key; if it hits
  $0 every user's gens fail. (No global pool guard yet.)
- **Person-driven (KYC) video needs Cloudflare R2** (four `R2_*` vars). Normal
  Avis Seedance video works without it. See [docs/r2_setup.md](docs/r2_setup.md).
- **Media** is stored locally under `.\storage` (fine to start; move to R2 if the
  disk fills). The database lives in the Docker volume `flowboard_pgdata`
  (Docker path) or `.\storage\flowboard.db` (SQLite path) — **back these up**.
- **Update:** `git pull; powershell -ExecutionPolicy Bypass -File .\deploy.ps1`
  (add `-NoDocker` if that's how you set up), then `nssm restart Flowboard`.
- **Fresh database:** this is a new host — it starts with an empty DB (no projects
  from the dev Mac). Users create their own projects after logging in.
