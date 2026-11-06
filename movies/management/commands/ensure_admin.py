import os
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model


class Command(BaseCommand):
    help = "Automatically ensure a superuser/admin account exists using environment variables or CLI options"

    def add_arguments(self, parser):
        parser.add_argument('--username', type=str, default=None, help='Superuser username')
        parser.add_argument('--email', type=str, default='', help='Superuser email')
        parser.add_argument('--password', type=str, default=None, help='Superuser password')

    def handle(self, *args, **options):
        User = get_user_model()
        username = (
            options['username']
            or os.getenv('DJANGO_SUPERUSER_USERNAME')
            or os.getenv('ADMIN_USERNAME')
            or os.getenv('ADMIN_USER')
            or os.getenv('SUPERUSER_USERNAME')
            or ''
        ).strip()

        password = (
            options['password']
            or os.getenv('DJANGO_SUPERUSER_PASSWORD')
            or os.getenv('ADMIN_PASSWORD')
            or os.getenv('ADMIN_PASS')
            or os.getenv('SUPERUSER_PASSWORD')
            or ''
        ).strip()

        email = (
            options['email']
            or os.getenv('DJANGO_SUPERUSER_EMAIL')
            or os.getenv('ADMIN_EMAIL')
            or 'admin@cineverse.local'
        ).strip()

        if not username or not password:
            self.stdout.write(self.style.WARNING(
                "DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD are not set in environment. "
                "Skipping automated admin creation/update."
            ))
            return

        # Case-insensitive search for existing user
        existing_user = User.objects.filter(username__iexact=username).first()

        if existing_user:
            existing_user.username = username
            existing_user.is_staff = True
            existing_user.is_superuser = True
            existing_user.is_active = True
            existing_user.set_password(password)
            if email:
                existing_user.email = email
            existing_user.save()
            self.stdout.write(self.style.SUCCESS(
                f"Superuser '{username}' successfully updated with the configured password and staff permissions!"
            ))
        else:
            User.objects.create_superuser(username=username, email=email, password=password)
            self.stdout.write(self.style.SUCCESS(
                f"Superuser '{username}' created successfully!"
            ))
