#!/usr/bin/env bash
# Exit on error
set -o errexit

# Install python dependencies
pip install -r requirements.txt

# Collect static files with Whitenoise
python manage.py collectstatic --no-input

# Run database migrations
python manage.py migrate --no-input

# Automatically create or verify the superuser from env variables
python manage.py ensure_admin
