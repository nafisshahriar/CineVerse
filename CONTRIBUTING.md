# Contributing to CineVerse

Thank you for your interest in contributing to **CineVerse**!

## Development Setup

```bash
git clone https://github.com/nafisshahriar/CineVerse.git
cd CineVerse
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python manage.py migrate
python manage.py runserver
```

## Running Tests & Checks

CineVerse tags live external-API integration tests (`TMDB` / `Cinemeta` / `IMDb`) with `@tag('network')` so that local development and CI remain fast and deterministic.

```bash
# Run Django system checks, migration verification, and offline test suite:
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test --exclude-tag=network -v 2

# Optional: run full test suite including live network tests (requires internet + TMDB_API_KEY):
python manage.py test
```

Notice that `moviedash/settings.py` automatically isolates `manage.py test` onto an in-memory SQLite database unless `TEST_DATABASE_URL` is explicitly set, protecting any Supabase or production `DATABASE_URL` configured in `.env`.

## Database & Migration Guidelines

- Keep models and standard migrations compatible with **both SQLite and PostgreSQL**.
- Put PostgreSQL-specific optimizations (such as `pg_trgm` GIN indexes or partial indexes) inside `migrations.RunPython(...)` gated on `schema_editor.connection.vendor == 'postgresql'`, with an explicit `reverse_code` and a graceful fallback if extensions cannot be created.
- Never commit `.env`, API keys, or local `*.sqlite3` database files.

## Submitting Pull Requests

1. Fork the repository and create a feature branch (`git checkout -b feat/your-feature`).
2. Add tests covering new endpoints, query optimizations, or bug fixes in `movies/tests.py`.
3. Verify `python manage.py check`, `makemigrations --check --dry-run`, and `python manage.py test --exclude-tag=network` pass with zero errors.
4. Open a concise pull request describing the change and any configuration impact.
