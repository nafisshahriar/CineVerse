import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', 'dev-secret-key-change-in-production')
DEBUG = os.getenv('DJANGO_DEBUG', 'True').lower() in ('true', '1', 'yes')

ALLOWED_HOSTS = [host.strip() for host in os.getenv('DJANGO_ALLOWED_HOSTS', '*').split(',') if host.strip()]
RENDER_EXTERNAL_HOSTNAME = os.getenv('RENDER_EXTERNAL_HOSTNAME')
if RENDER_EXTERNAL_HOSTNAME and RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)

# Trusted origins for CSRF protection behind reverse proxies (Render, Cloudflare, custom domains)
CSRF_TRUSTED_ORIGINS = [
    'https://*.onrender.com',
    'https://*.trycloudflare.com',
    'http://localhost:8000',
    'http://127.0.0.1:8000',
]
if RENDER_EXTERNAL_HOSTNAME:
    CSRF_TRUSTED_ORIGINS.append(f"https://{RENDER_EXTERNAL_HOSTNAME}")
if os.getenv('CSRF_TRUSTED_ORIGINS'):
    CSRF_TRUSTED_ORIGINS.extend([o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS').split(',') if o.strip()])

# Tell Django it is behind a reverse proxy (Render load balancer) that handles SSL termination
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'movies',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    # Compress dynamic HTML/JSON responses (Django >= 4.2 includes BREACH mitigation)
    'django.middleware.gzip.GZipMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'moviedash.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'moviedash.wsgi.application'

# Database configuration: PostgreSQL via DATABASE_URL or SQLite with WAL mode
# When running `manage.py test`, isolate onto local SQLite unless TEST_DATABASE_URL is explicitly set
RUNNING_TESTS = len(sys.argv) > 1 and sys.argv[1] == 'test'
_RAW_DB_URL = os.getenv('TEST_DATABASE_URL', '') if RUNNING_TESTS else os.getenv('DATABASE_URL', '')
DATABASE_URL = _RAW_DB_URL.strip().strip('"').strip("'")
if DATABASE_URL:
    import dj_database_url
    DATABASES = {
        'default': dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=int(os.getenv('DB_CONN_MAX_AGE', '60')),
            conn_health_checks=True,
        )
    }
    if DATABASES['default'].get('ENGINE', '').endswith('postgresql'):
        _pg_options = DATABASES['default'].setdefault('OPTIONS', {})
        # Fail fast instead of hanging when the remote database is unreachable
        _pg_options.setdefault('connect_timeout', int(os.getenv('DB_CONNECT_TIMEOUT', '10')))
        # Detect half-open TCP connections dropped by NAT / poolers
        _pg_options.setdefault('keepalives', 1)
        _pg_options.setdefault('keepalives_idle', 30)
        _pg_options.setdefault('keepalives_interval', 10)
        _pg_options.setdefault('keepalives_count', 3)
        # Identify CineVerse connections in pg_stat_activity
        _pg_options.setdefault('application_name', 'cineverse')
else:
    DATABASES = {
        'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': Path(os.getenv('SQLITE_DB_PATH', str(BASE_DIR / 'db.sqlite3'))),
            'OPTIONS': {
                'init_command': (
                    'PRAGMA journal_mode=WAL;'
                    'PRAGMA synchronous=NORMAL;'
                    'PRAGMA busy_timeout=15000;'
                    'PRAGMA cache_size=-64000;'
                ),
                'transaction_mode': 'IMMEDIATE',
            },
        }
    }

AUTH_PASSWORD_VALIDATORS = []

# In-process cache for cheap, slowly-changing catalog stats (header counts, source list).
# Tests use a dummy cache so every test sees fresh database state.
CACHES = {
    'default': {
        'BACKEND': (
            'django.core.cache.backends.dummy.DummyCache' if RUNNING_TESTS
            else 'django.core.cache.backends.locmem.LocMemCache'
        ),
        'LOCATION': 'cineverse-default',
        'TIMEOUT': 300,
    }
}
CATALOG_STATS_CACHE_SECONDS = int(os.getenv('CATALOG_STATS_CACHE_SECONDS', '60'))

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATICFILES_DIRS = [BASE_DIR / 'static']
STATIC_ROOT = BASE_DIR / 'staticfiles'

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# Whitenoise fail-safe: automatically find uncollected static files in STATICFILES_DIRS
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# Management & Maintenance Dashboard Access Control
# If MANAGE_REQUIRE_AUTH is True (default when DEBUG=False), visiting /manage/ or /manage/api/
# requires an authenticated staff user or a valid MANAGE_SECRET_TOKEN.
MANAGE_REQUIRE_AUTH = os.getenv('MANAGE_REQUIRE_AUTH', str(not DEBUG)).lower() in ('true', '1', 'yes')
MANAGE_SECRET_TOKEN = os.getenv('MANAGE_SECRET_TOKEN', '').strip()
