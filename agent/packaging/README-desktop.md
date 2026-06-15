# Flowboard (desktop build)

A self-contained Flowboard for video generation via the Avis Seedance 2.0 API.
No install, no database server, no Docker — everything runs locally.

## Setup (one time)

1. Put `Flowboard.exe` in its own folder.
2. Rename `.env.example` to `.env` (keep it in the same folder as the exe) and
   paste your **Avis API key** into `AVIS_API_KEY=`.

## Run

Double-click `Flowboard.exe`. A console window opens and your browser opens to
`http://127.0.0.1:8101`. Close the console window to stop the app.

## Where data lives

A `data/` folder is created next to the exe:

- `data/flowboard.db` — your projects/scenes/shots (SQLite).
- `data/media/` — uploaded images and generated video files.

To move or back up your work, copy the whole folder (exe + `.env` + `data/`).

## Notes

- An **internet connection is required** — video generation runs on the Avis
  cloud (your key, your credits).
- The Google Flow extension and Flow-based image generation are **not** part of
  this build. Use **Upload** to bring in reference images (character sheets,
  environments); generation runs through Avis Seedance 2.0.
- If port 8101 is taken, set `FLOWBOARD_HTTP_PORT=<port>` in `.env`.
