# WinServerRAG installer build

Produces `dist\WinServerRAG-Setup-<VERSION>.exe` (~600 MB), a one-shot
Windows installer that drops the 4 service exes + Electron mini-monitor
into `%ProgramFiles%\WinServerRAG\`, registers two NSSM-backed Windows
services (`WinServerRAG-API`, `WinServerRAG-Daemon`), and writes
config/log/backup dirs under `%ProgramData%\WinServerRAG\`.

## Layout

```
installer/
├── pyinstaller/
│   ├── _common.py              # Shared hidden-imports / data helpers
│   ├── api.spec                # winserverrag-api.exe
│   ├── daemon.spec             # winserverrag-daemon.exe
│   ├── dbinit.spec             # winserverrag-dbinit.exe
│   ├── backup.spec             # winserverrag-backup.exe (replaces backup.bat)
│   └── hooks/
│       ├── rt_utf8.py          # Force UTF-8 stdio
│       └── rt_omp_threads.py   # Cap OMP/MKL=1 (daemon only)
├── inno/
│   ├── WinServerRAG.iss        # Inno Setup 6 script
│   └── assets/
│       ├── icon.ico            # Installer icon
│       └── nssm.exe            # Bundled — auto-downloaded by build.ps1
├── build.ps1                   # End-to-end build pipeline
└── README.md                   # this file
```

## Prereqs (build machine, one-time)

- **Python 3.12** with project venv (`.venv\Scripts\pyinstaller` available)

  ```powershell
  python -m venv .venv
  .\.venv\Scripts\activate
  pip install -r requirements.txt
  pip install pyinstaller
  ```

- **Node.js 20+** (`node`, `npm` on PATH)

- **Inno Setup 6** — install from <https://jrsoftware.org/isdl.php> to
  the default location (`C:\Program Files (x86)\Inno Setup 6\`).

`nssm.exe` is auto-downloaded on first build and cached in
`installer/inno/assets/`.

## Build

```powershell
pwsh installer\build.ps1
```

Output: `dist\WinServerRAG-Setup-1.2.0.exe` (~600 MB).

### Switches

| Switch | What |
|---|---|
| `-CleanFirst` | wipe `dist/` and `build/` before build |
| `-SkipPython` | only re-pack mini + installer (PyInstaller is slow, ~5 min) |
| `-SkipElectron` | only re-pack python + installer |
| `-Version 1.2.0` | override the version baked into the installer filename |

### Per-stage timing (Akasaka PC)

| Stage | Time |
|---|---|
| PyInstaller × 4 | ~5–8 min (cold), ~1 min (incremental) |
| Electron pack | ~30 s |
| Inno Setup | ~30 s |
| **Total cold** | ~7–10 min |

## What the installer does at install time

1. Copies the 4 PyInstaller bundles to `%ProgramFiles%\WinServerRAG\bin\winserverrag-{api,daemon,dbinit,backup}\`.
2. Copies the Electron mini-monitor to `%ProgramFiles%\WinServerRAG\mini\`.
3. Drops `nssm.exe` at `%ProgramFiles%\WinServerRAG\bin\nssm.exe`.
4. Creates `%ProgramData%\WinServerRAG\{config,logs,backups\daily,backups\weekly}\`.
5. Copies `config.v2.env.example` to `%ProgramData%\WinServerRAG\config\`.
6. Registers two services via NSSM:
   - `WinServerRAG-API` (auto-start)
   - `WinServerRAG-Daemon` (auto-start, `DependOnService=WinServerRAG-API`)
   Both rotate logs at 10 MB.
7. Adds Start Menu shortcuts (Mini Monitor, Web Console).

## What the installer does NOT do

- **Install PostgreSQL 17** — prereq check warns the user if `postgresql-x64-17`
  service is missing. Operator installs PG manually from
  <https://www.postgresql.org/download/windows/>.
- **Install pgvector** — operator builds from source on first run
  (see `docs/EVOLUTION.md`).
- **Install NVIDIA CUDA driver** — CPU-only torch is bundled. For GPU
  acceleration (4-15× speedup), the operator post-install:
  ```powershell
  cd "C:\Program Files\WinServerRAG\bin\winserverrag-daemon\_internal"
  .\python.exe -m pip uninstall torch -y
  .\python.exe -m pip install torch==2.6.0 --index-url https://download.pytorch.org/whl/cu124
  # restart the WinServerRAG-Daemon service
  ```
  This is a known limitation of CPU-default builds; a GPU-bundled
  variant is on the v1.3 roadmap.
- **Set up OAuth credentials** — operator places `credentials.json` and
  `token.pickle` under `%ProgramData%\WinServerRAG\config\` after install.
- **Run db_init** — operator runs `winserverrag-dbinit.exe` once after
  editing `config.v2.env` (PostgreSQL must be ready first).

## Post-install operator checklist

1. Edit `%ProgramData%\WinServerRAG\config\config.v2.env`
   (copy from `config.v2.env.example`).
2. Place `credentials.json` (Google OAuth client) in the same dir.
3. Run `winserverrag-dbinit.exe` once (creates DB + applies schema).
4. Start the services:
   ```powershell
   sc start WinServerRAG-API
   sc start WinServerRAG-Daemon
   ```
5. Open <http://127.0.0.1:17600/> for the web console.
6. Optional: launch mini-monitor from Start Menu.

## Uninstall

The uninstaller stops + deletes both services, removes the install dir,
and **preserves** `%ProgramData%\WinServerRAG\` (logs, backups, config).
For a full wipe:

```powershell
Remove-Item -Recurse -Force "$env:ProgramData\WinServerRAG"
```

## Troubleshooting

- **PyInstaller "ModuleNotFoundError: ..." at runtime** — add the missing
  module to `COMMON_HIDDEN_IMPORTS` in
  `installer/pyinstaller/_common.py`, rebuild.
- **Smart Screen warning on first run** — expected, no code-signing cert
  in this build line. Click "詳細情報 → 実行" once.
- **Service fails to start** — check `%ProgramData%\WinServerRAG\logs\
  api.err.log` / `daemon.err.log`. Most common cause: PostgreSQL not
  running, or `config.v2.env` has wrong PG password.
- **`PyInstaller` AV false-positive** — also expected for unsigned exes.
  Add an exclusion for `%ProgramFiles%\WinServerRAG\` in Defender.

## See also

- [docs/EVOLUTION.md](../docs/EVOLUTION.md) — project history
- [scripts/install_service.md](../scripts/install_service.md) —
  manual NSSM registration (legacy, pre-installer)
- [CHANGELOG.md](../CHANGELOG.md) — release notes
