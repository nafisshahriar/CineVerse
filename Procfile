web: python manage.py migrate --noinput && python manage.py collectstatic --noinput && python manage.py ensure_admin && gunicorn moviedash.wsgi:application --log-file -
