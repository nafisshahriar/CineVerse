from django.core.management.base import BaseCommand
from django.db import transaction
from urllib.parse import unquote
import os
from movies.models import Movie
from movies.utils import parse_movie_info, ProgressTracker


class Command(BaseCommand):
    help = 'Backfill source, resolution, quality, language, clean_title, and group_key for all movies'

    def add_arguments(self, parser):
        parser.add_argument('--batch-size', type=int, default=1000, help='Batch size for bulk updates (default: 1000)')
        parser.add_argument('--force', action='store_true', help='Recompute even if already populated')

    def handle(self, *args, **options):
        batch_size = options['batch_size']
        force = options['force']

        qs = Movie.objects.select_related('detail').all()
        if not force:
            qs = qs.filter(clean_title='')

        total = qs.count()
        if total == 0:
            self.stdout.write(self.style.SUCCESS('All movies already have metadata populated! Use --force to reprocess.'))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"\nPopulating metadata for {total} movies (batch size: {batch_size})..."))
        tracker = ProgressTracker(total, desc="Backfilling")

        # Process in chunks
        all_ids = list(qs.values_list('id', flat=True))
        updated_count = 0

        for i in range(0, len(all_ids), batch_size):
            chunk_ids = all_ids[i:i + batch_size]
            movies = list(Movie.objects.filter(id__in=chunk_ids).select_related('detail'))
            
            for m in movies:
                file_name = os.path.basename(unquote(m.file_url or ''))
                dir_name = os.path.basename(unquote(m.directory_url or '').rstrip('/\\'))
                if not dir_name and m.title:
                    dir_name = m.title

                tmdb_lang = ''
                try:
                    if hasattr(m, 'detail') and m.detail and m.detail.original_language:
                        tmdb_lang = m.detail.original_language
                except Exception:
                    pass

                info = parse_movie_info(
                    dir_name=dir_name,
                    file_name=file_name,
                    file_url=m.file_url or '',
                    tmdb_lang=tmdb_lang
                )

                m.clean_title = info['clean_title']
                m.source = info['source']
                m.resolution = info['resolution']
                m.quality = info['quality']
                m.language = info['language']
                m.group_key = info['group_key']

                # Also fix year if it was missing or 0 but parsed now
                if not m.year and info['year']:
                    m.year = info['year']
                    m.year_str = info['year_str']

            with transaction.atomic():
                Movie.objects.bulk_update(
                    movies,
                    ['clean_title', 'source', 'resolution', 'quality', 'language', 'group_key', 'year', 'year_str']
                )

            updated_count += len(movies)
            log_line = tracker.tick(
                success=True,
                title=f"Batch {i // batch_size + 1}",
                extra=f"{updated_count}/{total} updated"
            )
            self.stdout.write(log_line)

        self.stdout.write(self.style.SUCCESS(f"\n[OK] Successfully populated metadata for {updated_count} movies!"))
