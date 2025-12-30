#!/bin/bash
# cineverse_startup.sh
# Adjust the path below to where your project is on the Pi
PROJECT_DIR="/home/nafis/CineVerse"

# 1. Navigate to project
cd "$PROJECT_DIR" || exit

# 2. Wait for internet connection (simple check)
until ping -c 1 google.com &> /dev/null; do
    echo "Waiting for network..."
    sleep 5
done

# 3. Pull latest code
echo "Pulling latest code..."
git reset --hard HEAD # CAUTION: Discards local changes to ensure clean pull
git pull origin main

# 4. Activate Virtual Environment
source .CinePi/bin/activate

# 5. Install any new dependencies
pip install -r requirements.txt

# 6. Apply database migrations
python manage.py migrate

# 7. Collect static files
python manage.py collectstatic --noinput

# 8. Start Gunicorn
# Using full path to gunicorn from virtual environment
echo "Starting Server..."
exec "$PROJECT_DIR/.CinePi/bin/gunicorn" moviedash.wsgi:application --bind 0.0.0.0:8000
