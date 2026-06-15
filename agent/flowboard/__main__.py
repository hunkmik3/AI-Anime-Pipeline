"""Desktop entrypoint — single-process Flowboard (SQLite + bundled SPA).

This is the PyInstaller entry script (also runnable via ``python -m flowboard``
for an unfrozen smoke test). It:

1. Loads ``.env`` from next to the executable (frozen) so the recipient drops
   their own ``AVIS_API_KEY`` beside the exe.
2. Forces bundled mode (SQLite on disk, Flow bridge off, FastAPI serves the SPA).
3. Starts the API + UI on one port and opens the default browser.

Data (SQLite DB + media cache) lives in ``<exe dir>/data`` — see config.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path


def _load_env_beside_exe() -> None:
    """Frozen build: load ``.env`` sitting next to the executable (override=False
    so a real shell env still wins). No-op when the file isn't there."""
    if not getattr(sys, "frozen", False):
        return
    env_path = Path(sys.executable).resolve().parent / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv

            load_dotenv(env_path, override=False)
        except Exception:  # noqa: BLE001 — missing dotenv shouldn't crash boot
            pass


def main() -> None:
    _load_env_beside_exe()
    # Implied by sys.frozen, but set so `python -m flowboard` also takes the
    # bundled path (SQLite + bridge off + static SPA).
    os.environ.setdefault("FLOWBOARD_BUNDLED", "1")

    import uvicorn

    # Import after env is set so config resolves bundled defaults correctly.
    from flowboard.config import HTTP_PORT
    from flowboard.main import app

    url = f"http://127.0.0.1:{HTTP_PORT}"

    def _open_browser() -> None:
        time.sleep(1.5)  # give uvicorn a moment to bind
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=_open_browser, daemon=True).start()
    print(f"\n  Flowboard running → {url}\n  (close this window to stop)\n")
    uvicorn.run(app, host="127.0.0.1", port=HTTP_PORT, log_level="info")


if __name__ == "__main__":
    main()
