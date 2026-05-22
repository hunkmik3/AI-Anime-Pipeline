# Cloudflare R2 Setup (Phase 5, updated Phase 6.5)

Dreamina's video API consumes reference images via `image_url.url` — the
URL must be **publicly reachable** from BytePlus's servers. Flowboard's
local `/media/{id}` route only serves to localhost, so we mirror each
reference image to Cloudflare R2 before submitting to the upstream API.

## TL;DR — where credentials live

`.env` at the repo root is the **canonical source of truth** for
Flowboard runtime config (Phase 6.5). The agent calls `load_dotenv()`
at boot and reads env vars first. `~/.flowboard/secrets.json` is a
**legacy local cache** that still works as a fallback for installs
that pre-date the migration; it is planned for deprecation in Phase 8.

Env vars (canonical):

```bash
# .env (gitignored, repo root)
BYTEPLUS_KEY=ark-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX-XXXXX
R2_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=flowboard-anime-dev
# R2_PUBLIC_BASE_URL=https://pub-<hash>.r2.dev  # optional
```

Resolution order in [`agent/flowboard/services/llm/secrets.py`](../agent/flowboard/services/llm/secrets.py):

1. Process env (populated from `.env` at boot, or shell exports).
2. Field-by-field fallback to `~/.flowboard/secrets.json`.

This means a partial migration works — you can leave `R2_BUCKET` in
.env while keeping the rotated secret in secrets.json. The two stores
merge per-field.

R2 is S3-compatible and ships a free tier that's more than enough for
single-user prosumer use:

- **Storage**: 10 GB / month free
- **Class A ops** (writes / list): 1 M / month free
- **Class B ops** (reads): 10 M / month free
- **Egress**: free (this is the headline difference vs. AWS S3)

## 1. Create a Cloudflare account

If you already have one, skip. Otherwise:

1. Visit https://dash.cloudflare.com/sign-up
2. Verify email
3. Choose the **Free** plan when prompted for a tier — R2 is opt-in
   from the dashboard regardless

## 2. Enable R2

1. In the Cloudflare dashboard left nav, click **R2 Object Storage**
2. Click **Purchase R2 Plan** (the free tier is implicitly the "Free"
   variant — you won't be charged until you exceed the free tier)
3. Confirm payment method (required even for the free tier — Cloudflare
   bills overages without warning)

## 3. Create a bucket

1. In **R2 → Overview**, click **Create bucket**
2. Name: `flowboard-media` (or any name; keep it short, lowercase,
   hyphen-only)
3. Location: pick the closest jurisdiction (APAC / WNAM / EEUR / ENAM).
   This affects upload latency from your dev machine; it does NOT
   affect Dreamina (BytePlus pulls the URL over the public internet
   regardless)
4. **Don't** enable Public Access yet — we'll use presigned URLs
   (1-hour expiry per request); presigned URLs work even when the
   bucket is private

## 4. Create an API token

R2 uses an S3-compatible token, distinct from your Cloudflare Global
API key.

1. **R2 → Manage R2 API Tokens**
2. Click **Create API token**
3. Token name: `flowboard-dreamina-upload`
4. Permissions: **Object Read & Write**
5. Specify bucket: select your bucket (`flowboard-media`)
6. TTL: leave empty (or set to a fixed expiry if you want to rotate)
7. Click **Create API Token**
8. Copy:
   - **Access Key ID**
   - **Secret Access Key**
   - **Endpoint** (looks like `https://<account_id>.r2.cloudflarestorage.com`)

The secret is shown **only once** — paste into a password manager or
straight into `~/.flowboard/secrets.json` (next step) immediately.

## 5. Wire credentials into Flowboard

### Preferred (Phase 6.5): `.env` at the repo root

Create or edit `flowboard/.env`. The file is gitignored — safe for
secrets:

```bash
# Dreamina / Seedance API key (Volcengine Ark)
BYTEPLUS_KEY=ark-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX-XXXXX
# Alias accepted by the secrets layer — pick one or set both:
# DREAMINA_API_KEY=ark-...

# Cloudflare R2
R2_ENDPOINT_URL=https://<account_id>.r2.cloudflarestorage.com
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
R2_BUCKET=flowboard-media
# Optional public CDN passthrough (see below):
# R2_PUBLIC_BASE_URL=https://pub-<hash>.r2.dev
```

`R2_ENDPOINT_URL` is the bucket-agnostic R2 host — the bucket name
goes into `R2_BUCKET`, NOT the URL. Boto3 builds the final URL as
`<endpoint>/<bucket>/<key>` internally.

The agent autoloads `.env` at boot via `python-dotenv`; no shell
sourcing needed. Restart the agent after editing.

`R2_PUBLIC_BASE_URL` enables the Cloudflare R2 public dev URL (publicly
served without presigning) — Flowboard then skips presigning and
returns the public URL directly. **Don't use this in production** —
it disables read-access control.

### Legacy fallback: `~/.flowboard/secrets.json`

Still supported for back-compat. Same shape as before:

```json
{
  "apiKeys": {
    "dreamina": "ark-XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX-XXXXX"
  },
  "r2": {
    "endpoint_url": "https://<account_id>.r2.cloudflarestorage.com",
    "access_key_id": "...",
    "secret_access_key": "...",
    "bucket": "flowboard-media",
    "public_base_url": "https://media.example.com"
  }
}
```

Resolution: any field set in `.env` overrides the corresponding
secrets.json field. Fields unset in `.env` fall back to secrets.json.

**Planned deprecation**: Phase 8 will remove the secrets.json fallback
once .env is the only documented path. New installs should skip
secrets.json entirely.

## 6. Verify

Run the smoke test (see `docs/phase5_smoke_tests.md`):

```bash
cd agent
.venv/bin/python -c "
from pathlib import Path
from flowboard.services.storage import prepare_image_url
# Use any local image you already have cached
test_img = Path('storage/media').glob('*.png').__next__()
url = prepare_image_url(test_img)
print(url)
"
```

You should see a long presigned URL on stdout. Open it in a browser
(or `curl -I`) — HTTP 200 means the upload + presign chain works.

## Cost monitoring

The free tier is generous but not unlimited. To stay within it:

- A 1024×1024 PNG is roughly 1 MB. 10 GB / month free → 10,000
  uploads / month with bytes-only accounting; the metadata ops are
  separately metered against the 1M Class A op limit
- A 30 s Dreamina job = 1 ref image = 1 upload + 1 presign. So
  ~10,000 jobs / month is the bytes-side ceiling

If you exceed, R2 bills $0.015 / GB-month for storage and
$4.50 / M Class A ops — still cheaper than S3 by ~70% (no egress).

## Migration to a different S3 backend

If you'd rather use AWS S3, MinIO (self-hosted), or Backblaze B2,
the `ObjectStorage` Protocol at `agent/flowboard/services/storage/base.py`
abstracts the bucket. Drop in a new class with the same shape and point
`get_default_storage()` at it. No worker / provider code change needed.

## Troubleshooting

**`SignatureDoesNotMatch` on upload**: clock skew between your machine
and Cloudflare. Run `sudo sntp -sS time.apple.com` (macOS) or
`sudo ntpdate pool.ntp.org` (Linux).

**`NoSuchBucket`**: bucket name in `secrets.json` doesn't match what
you created. Names are case-sensitive; check the dashboard.

**Dreamina returns `error_bad_image`**: the presigned URL expired
before BytePlus fetched it. Increase `expires_seconds` in
`storage/base.py` from 3600 (1h) to e.g. 14400 (4h). The Dreamina job
itself takes 90-220 s; 1h is comfortably more, but if your
upload-to-submit window stretches due to queue depth, bump the expiry.
