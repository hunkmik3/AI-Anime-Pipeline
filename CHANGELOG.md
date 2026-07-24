# Giant Studio — Release Notes

> App: https://giantstudio.reelmind.co · Source branch: `anime-adaptation`
> Each release lists **what changed relative to the previous version**.

---

## v1.0.2 — 2026-07-20

### ⚡ Performance
- **Big speed-up for projects with many sequences or clips.** Video tiles on the canvas and in the "Generated videos" gallery now show a lightweight still thumbnail (a WebP of the clip's first frame) instead of loading a full `<video>` element per tile — dozens of clips no longer spin up dozens of video decoders. Thumbnails are generated once and cached.
- The canvas now only renders the sequences/nodes currently on screen (off-screen ones aren't mounted), so panning and zooming a large episode stays smooth.
- **Fix: "extract continuity frame" now works** — the server-side ffmpeg tool it relies on wasn't reachable from the service; extracting a still frame from a clip works again (and powers the new video thumbnails).

### 🔔 New
- **In-app notifications** — a bell in the header announces product updates, grouped per release; click an update to expand its full changelog. (Role-aware: admins also see admin-only notes.)
- **Download from the gallery** — every clip in a project's "Generated videos" section now has a download button on its thumbnail.

### 🎬 Video generation
- **Seedance 2.0 reference audio** — attach a voice/audio reference (mp3 / wav) to guide a clip. Supports multiple `@audioN` voices, with a ~15-second total-audio pre-flight check that warns before generating.

---

## v1.0.1 — 2026-07-19

First numbered release. Highlights of the features and fixes shipped in this build:

### 🎬 Video generation
- **Fix: the resolution you pick is now the resolution you get.** Previously every clip fell back to 720p regardless of the Resolution dropdown; the selected value (480p / 720p / 1080p) is now sent to the model **and recorded** for reporting.
- **Added 4:3 and 1:1 aspect ratios** for video (alongside 16:9 and 9:16).
- The reference-label dropdown on ref nodes now goes up to **@image10**.
- **4K disabled** across all models.

### 🧩 Video models
- **Added:** Seedance 1.5 Pro · Seedance 2.0 Fast · Seedance 2.0 Mini · Seedance 1.0 Pro · Seedance 1.0 Pro Fast (all via Avis).
- **Removed:** Google Flow · Seedance 2.0 BytePlus (direct).
- All models: full 480p / 720p / 1080p resolutions (no 4K).

### 📽️ History & clip gallery
- **Per-node generation history** (⏱ button on video nodes): review every take — thumbnail, status, cost. The button is now larger and clearer.
- Clips in history are **playable & downloadable** directly.
- **New — "Generated videos" gallery on the project page:** all clips grouped by **Episode → Sequence**, with inline playback plus the **prompt, settings, and the reference images used**.

### 🛠️ Admin dashboard
- The cost breakdown now shows each generation's **real resolution** (720p / 1080p…), reconciled across Overview / model stats / cost tree.
- **Full mobile redesign:** tables become stacked cards (Members, Projects, Audit log), a **slide-in drawer menu (hamburger)**, and the horizontal-scroll drift on Overview is fixed.

### 🔁 Reliability
- When Avis returns **"api key gen limit reached" (HTTP 429)**, the request is **retried with backoff** instead of failing the generation; generation concurrency is throttled to stay under the key's rate limit.

### 🖼️ Assets & thumbnails
- Fixed **broken thumbnails** in the Asset Library (the thumbnail route was wrongly blocked by the auth gate).

### ✨ UI & branding
- Version label **v1.0.1** + **Giant Studio** logo/name in the Projects sidebar and Admin console.

### 📦 Platform (included in this build)
- Self-service signup + admin approval + **credentials emailed** (SMTP).
- **Google Workspace SSO** login.
- **Audit log** (logins, admin actions) + a cost / waste analytics console.
- English UI, concurrent generation worker.
