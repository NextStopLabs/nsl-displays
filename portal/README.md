# Portal (`portal/`)

FastAPI backend serving under `/portal` (plan.md §8) + legacy-style static frontend.
SQLite locally (`DATABASE_URL=sqlite:./dev.db`), Postgres in prod (same schema).

Run dev (no prefix locally; the proxy strips `/portal` in prod - see `deploy/config/Caddyfile`):
`uvicorn portal.backend.app:app --host 127.0.0.1 --port 8000`, then open `http://127.0.0.1:8000/`.
Key flows: signup → pair controller (6-char code) → create program → services →
destinations (8-digit codes) → stops → layouts → publish (validates, builds
`.nslprog`, refuses no-change) → upload (queued per controller) → device pulls
manifest/bundle/audio with resume+hash → atomic install → activate at safe moment.
