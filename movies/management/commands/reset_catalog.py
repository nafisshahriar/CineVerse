import sys
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.core.management import call_command
from movies.models import Movie, MovieDetail, CrawledDirectory, FailedParse


class Command(BaseCommand):
    help = "Reset the movie catalog to start fresh with your own media library."

    def add_arguments(self, parser):
        parser.add_argument(
            '--yes', '-y',
            action='store_true',
            help='Skip interactive confirmation prompt.'
        )
        parser.add_argument(
            '--keep-admin',
            action='store_true',
            default=True,
            help='Preserve existing administrator and staff accounts (default: True).'
        )
        parser.add_argument(
            '--crawl',
            type=str,
            default=None,
            help='Optional H5AI / directory URL to immediately crawl after resetting.'
        )

    def handle(self, *args, **options):
        skip_confirm = options['yes']
        crawl_url = options['crawl']

        movie_count = Movie.objects.count()
        detail_count = MovieDetail.objects.count()
        crawl_count = CrawledDirectory.objects.count()

        self.stdout.write(self.style.WARNING(
            "\n======================================================\n"
            "   ⚠️  CineVerse Database Reset & Fresh Setup\n"
            "======================================================\n"
        ))
        self.stdout.write(f"Current catalog contents:")
        self.stdout.write(f" - Movies:             {movie_count}")
        self.stdout.write(f" - Detailed Metadata:  {detail_count}")
        self.stdout.write(f" - Crawled Directories:{crawl_count}\n")

        if not skip_confirm:
            self.stdout.write(self.style.NOTICE(
                "This will permanently delete all movie records, metadata, and crawl history.\n"
                "Your admin credentials and database schema will be preserved."
            ))
            try:
                confirm = input("Are you sure you want to proceed? [y/N]: ").strip().lower()
            except (KeyboardInterrupt, EOFError):
                self.stdout.write("\nOperation cancelled.")
                return

            if confirm not in ('y', 'yes'):
                self.stdout.write("Operation cancelled.")
                return

        self.stdout.write("\nResetting catalog records...")

        with transaction.atomic():
            FailedParse.objects.all().delete()
            CrawledDirectory.objects.all().delete()
            MovieDetail.objects.all().delete()
            Movie.objects.all().delete()

            # Reset auto-increment counters on SQLite
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "DELETE FROM sqlite_sequence WHERE name IN "
                        "('movies_movie', 'movies_moviedetail', 'movies_crawleddirectory', 'movies_failedparse')"
                    )
            except Exception:
                pass

        self.stdout.write(self.style.SUCCESS(
            "✓ Catalog successfully reset! The database is now clean and empty."
        ))

        if crawl_url:
            self.stdout.write(self.style.MIGRATE_HEADING(
                f"\nStarting initial import from: {crawl_url}..."
            ))
            try:
                call_command('update_all', url=crawl_url)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"Failed to crawl: {e}"))
        else:
            self.stdout.write(self.style.NOTICE(
                "\nNext steps to populate with your media:\n"
                "  1. Run crawler on your H5AI server / folder URL:\n"
                "     python manage.py update_all --url \"http://your-server/movies/\"\n"
                "  2. Or open the web Management Studio at:\n"
                "     http://localhost:8000/manage/\n"
            ))
