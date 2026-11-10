#!/usr/bin/env bash
# Exit on error
set -o errexit

# Upgrade pip and install python dependencies
python -m pip install --upgrade pip
pip install -r requirements.txt

# Collect static files with Whitenoise
python manage.py collectstatic --no-input

# Run database migrations
python manage.py migrate --no-input

# Automatically create or verify the superuser from env variables if configured
python manage.py ensure_admin || true
