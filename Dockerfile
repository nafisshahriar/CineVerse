FROM python:3.12-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . /app/

# Expose port
EXPOSE 8000

# Default entrypoint: run migrations, ensure admin account, collect static, and start gunicorn
CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py ensure_admin && python manage.py collectstatic --noinput && gunicorn moviedash.wsgi:application --bind 0.0.0.0:8000 --workers 3 --timeout 120"]
