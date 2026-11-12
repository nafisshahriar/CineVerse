#!/usr/bin/env bash
set -euo pipefail

# Defaults to repository root (override via CINEVERSE_DIR or systemd WorkingDirectory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="${CINEVERSE_DIR:-$(dirname "$SCRIPT_DIR")}"
VENV_DIR="${CINEVERSE_VENV:-$PROJECT_DIR/.venv}"

cd "$PROJECT_DIR"

# Optional auto-update before boot (disabled by default to preserve local changes)
if [ "${CINEVERSE_AUTO_PULL:-false}" = "true" ]; then
    echo "Pulling latest updates..."
    git pull --ff-only origin main || true
fi

if [ -f "$VENV_DIR/bin/activate" ]; then
    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
fi

python manage.py migrate --noinput
python manage.py collectstatic --noinput
python manage.py ensure_admin || true

echo "Starting CineVerse server with Gunicorn..."
exec gunicorn moviedash.wsgi:application \
    --bind "0.0.0.0:${PORT:-8000}" \
    --workers "${GUNICORN_WORKERS:-2}" \
    --threads "${GUNICORN_THREADS:-4}" \
    --timeout 120 \
    --log-file -
