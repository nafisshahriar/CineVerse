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
        username = options['username'] or os.getenv('DJANGO_SUPERUSER_USERNAME') or os.getenv('ADMIN_USERNAME')
        password = options['password'] or os.getenv('DJANGO_SUPERUSER_PASSWORD') or os.getenv('ADMIN_PASSWORD')
        email = options['email'] or os.getenv('DJANGO_SUPERUSER_EMAIL', 'admin@cineverse.local')

        if not username or not password:
            self.stdout.write(self.style.WARNING(
                "DJANGO_SUPERUSER_USERNAME and DJANGO_SUPERUSER_PASSWORD are not set. "
                "Skipping auto admin creation."
            ))
            return

        if User.objects.filter(username=username).exists():
            user = User.objects.get(username=username)
            updated = False
            if not user.is_staff or not user.is_superuser:
                user.is_staff = True
                user.is_superuser = True
                updated = True
            # Update password if requested in environment
            if os.getenv('UPDATE_ADMIN_PASSWORD', 'False').lower() == 'true':
                user.set_password(password)
                updated = True
            if updated:
                user.save()
                self.stdout.write(self.style.SUCCESS(f"Updated superuser flags for '{username}'."))
            else:
                self.stdout.write(self.style.SUCCESS(f"Superuser '{username}' already exists."))
        else:
            User.objects.create_superuser(username=username, email=email, password=password)
            self.stdout.write(self.style.SUCCESS(f"Superuser '{username}' created successfully!"))
