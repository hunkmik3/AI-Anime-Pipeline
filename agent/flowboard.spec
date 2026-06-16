# PyInstaller spec for the Flowboard self-contained desktop build.
#
# Run from the agent/ directory (CI does this), AFTER the frontend is built:
#     cd frontend && npm ci && npm run build
#     cd agent && pip install -e . pyinstaller && pyinstaller flowboard.spec
#
# Produces a single agent/dist/Flowboard.exe (onefile). On launch it serves the
# API + bundled SPA on one port and opens the browser. Data (SQLite + media)
# lives in <exe dir>/data; the recipient drops a .env (with AVIS_API_KEY) next
# to the exe. The Postgres driver is excluded — the bundle is SQLite-only.
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

hiddenimports = (
    collect_submodules("uvicorn")
    + collect_submodules("flowboard")
    + collect_submodules("PIL")
    + [
        "anyio",
        "sqlalchemy.dialects.sqlite",
        "sqlalchemy.sql.default_comparator",
    ]
)

# Bundle the built SPA. Resolved at runtime via sys._MEIPASS/frontend_dist
# (see flowboard.config._frontend_dist). Path is relative to this spec (agent/).
datas = [("../frontend/dist", "frontend_dist")]

a = Analysis(
    ["flowboard/__main__.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        "psycopg",          # bundle is SQLite-only; Postgres driver not needed
        "psycopg_binary",
        "psycopg2",
        "tkinter",
        "alembic",          # SQLite schema comes from SQLModel.create_all
    ],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Flowboard",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,            # keep a console window so logs are visible
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
