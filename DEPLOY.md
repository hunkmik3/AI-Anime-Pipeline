# Deploy Flowboard (multi-user) on a Mac Mini

One backend process serves **both the API and the web UI** on port `8101`
(it serves `frontend/dist` when present). Postgres runs in Docker. Cloudflare
Tunnel gives a public HTTPS URL. The Avis key stays **server-side**; users log
in with admin-provisioned accounts.

```
Browser ──HTTPS──> Cloudflare Tunnel ──> localhost:8101 (FastAPI + worker + SPA)
                                                   └── Postgres (Docker :15432)
                                                   └── Avis API (Seedance video)
```

## 1. Install tools (once)
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install git node python@3.12 cloudflared orbstack
# Open the OrbStack app once so the Docker engine starts.
```

## 2. Clone
```bash
git clone https://github.com/hunkmik3/AI-Anime-Pipeline.git ~/flowboard
cd ~/flowboard && git checkout anime-adaptation
```

## 3. Configure secrets
```bash
cp .env.example .env
# Fill AVIS_API_KEY and FLOWBOARD_ADMIN_PASSWORD. (deploy.sh fills SECRET_KEY.)
nano .env
```

## 4. One-command setup
```bash
./deploy.sh
```
It checks tools, creates the venv + installs the backend, starts Postgres and
migrates, and builds the frontend. Re-runnable after every `git pull`.
(First run, if `.env` was missing, it creates one and stops — fill it, run again.)

## 5. Run

**Test (foreground):**
```bash
cd ~/flowboard/agent && .venv/bin/uvicorn flowboard.main:app --host 0.0.0.0 --port 8101
```
Open <http://localhost:8101>, log in as `admin`.

**24/7 (launchd — auto-restarts, survives reboot):**
```bash
bash ~/flowboard/packaging/install-launchd.sh
# logs: ~/flowboard/logs/ ; remove: bash packaging/install-launchd.sh --uninstall
```

## 6. Public HTTPS (Cloudflare Tunnel)

**Quick (temporary URL):**
```bash
cloudflared tunnel --url http://localhost:8101    # prints https://xxx.trycloudflare.com
```

**Persistent (your own domain):**
```bash
cloudflared tunnel login
cloudflared tunnel create flowboard
cloudflared tunnel route dns flowboard app.yourdomain.com
# ~/.cloudflared/config.yml → service: http://localhost:8101 ; then:
cloudflared tunnel run flowboard
```

## 7. After it's up
1. Log in as admin → **change the password**.
2. **Quản lý tài khoản** → create users, set each user's **$ budget**.
3. Per user: **Hoạt động** (what they generated, cost, output), **Xoá** to remove.

## Operating notes
- ⚠️ **Sum of all user budgets ≤ real Avis balance** — one shared key; if it
  hits $0 every user's gens fail. (A global pool guard isn't built yet.)
- **Person-driven (KYC) video needs Cloudflare R2.** Normal Avis Seedance video
  works without it, but KYC uploads assets to a public URL → set the four
  `R2_*` vars in `.env` (see `.env.example` + `docs/r2_setup.md`). Without them,
  clicking KYC fails with *"needs public file hosting (R2)"*. You can reuse one
  R2 bucket across machines.
- **Media** is otherwise stored locally under `STORAGE` (fine on 256 GB to
  start; move to R2 later if it fills up).
- **Update:** `git pull && ./deploy.sh`, then restart the server
  (`launchctl unload/load` the plist, or just re-run uvicorn).
