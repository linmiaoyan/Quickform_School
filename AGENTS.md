# AGENTS.md

## Cursor Cloud specific instructions

### Project overview

QuickForm (`quickform.cn`) is a Python/Flask web application for form data collection and AI-powered analysis. Single-service architecture (not a monorepo).

Agent note: tiny doc-only change committed on `cursor/doc-touch-e466` to verify repo/branch routing.

### Running the dev server

1. Ensure a `.env` file exists at the repo root with at least `SECRET_KEY=<strong-random-value>`. Without MySQL env vars (`MYSQL_HOST`, `MYSQL_USER`, `MYSQL_PASSWORD`), the app falls back to SQLite automatically.
2. For local development, also set `SESSION_COOKIE_SECURE=false`, `REMEMBER_COOKIE_SECURE=false`, `FLASK_HOST=0.0.0.0`, and `FLASK_DEBUG=true` in `.env`.
3. Start the dev server: `python3 app.py` (listens on `FLASK_HOST:FLASK_PORT`, default `0.0.0.0:5000`).
4. Health check: `curl http://localhost:5000/ping` should return `pong`.

### Linting

No project-specific linter config exists. Use `python3 -m flake8 --max-line-length=150 app.py core/ services/` for basic checks. The existing codebase has style warnings (whitespace, line length) but no syntax errors.

### Testing

No automated test suite exists in this repo. Manual testing via the web UI is the primary method. Key flows: register → login → create task → submit data → AI analysis.

### System dependencies

`libcairo2-dev`, `pkg-config`, and `python3-dev` are required for building `pycairo` (transitive dependency of `xhtml2pdf`). These must be installed at the system level before `pip install -r requirements.txt`.

### Key gotchas

- The app raises `RuntimeError` on startup if `SECRET_KEY` is missing or set to the placeholder `your_secret_key_here`.
- SQLite DB file is created at `core/quickform.db` (relative to the blueprint module). It is gitignored.
- The `.env` file is gitignored and must be created manually.
- `pip install` may install to `~/.local/bin`; ensure this is on `PATH` if running CLI tools like `flask`, `flake8`, etc.
