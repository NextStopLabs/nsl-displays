# Portal (`portal/`)

FastAPI backend serving under `/portal` (plan.md §8) + legacy-style static frontend.
SQLite locally (`DATABASE_URL=sqlite:./dev.db`), Postgres in prod (same schema).

Run dev: `uvicorn portal.backend.app:app --root-path /portal`
Key flows: signup → pair controller (6-char code) → create program → services →
destinations (8-digit codes) → stops → layouts → publish (validates, builds
`.nslprog`, refuses no-change) → upload (queued per controller) → device pulls
manifest/bundle/audio with resume+hash → atomic install → activate at safe moment.
