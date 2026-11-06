from django.core.management.base import BaseCommand
from django.db import transaction
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from movies.models import Movie, MovieDetail
from movies.utils import fetch_imdb_rating_and_id, ProgressTracker


class Command(BaseCommand):
    help = 'Fetch authentic IMDb ratings and IMDb IDs (tt...) for movies in the database.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500, help='Max movies to process (default: 500)')
        parser.add_argument('--all', action='store_true', help='Process all movies missing IMDb ratings')
        parser.add_argument('--workers', type=int, default=8, help='Concurrent worker threads (default: 8)')
        parser.add_argument('--force', action='store_true', help='Re-fetch even if already has IMDb rating')

    def handle(self, *args, **options):
        limit = options['limit']
        fetch_all = options['all']
        workers = options['workers']
        force = options['force']

        qs = Movie.objects.filter(is_primary_copy=True)
        if not force:
            qs = qs.filter(imdb_rating__isnull=True)

        # Prioritize by popularity first, then year
        qs = qs.order_by('-popularity', '-year')

        if not fetch_all and limit:
            movies = list(qs[:limit])
        else:
            movies = list(qs)

        total = len(movies)
        if total == 0:
            self.stdout.write(self.style.SUCCESS('\n[OK] All targeted movies already have IMDb ratings!'))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nFetching authentic IMDb ratings for {total} movies ({workers} threads)...'
        ))
        tracker = ProgressTracker(total, desc="IMDb Ratings")

        def process_one(m):
            known_id = m.imdb_id or ''
            try:
                if hasattr(m, 'detail') and m.detail and m.detail.imdb_id:
                    known_id = m.detail.imdb_id
            except Exception:
                pass

            title = m.clean_title or m.title
            res = fetch_imdb_rating_and_id(title, m.year, known_imdb_id=known_id)
            rating = res[0]
            imdb_id = res[1] if len(res) > 1 else None
            genres = res[2] if len(res) > 2 else []
            return m.id, rating, imdb_id, genres, title

        success_count = 0
        batch_to_update = []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_movie = {executor.submit(process_one, m): m for m in movies}

            for future in as_completed(future_to_movie):
                m_id, rating, imdb_id, genres, title = future.result()
                ok = rating is not None
                if ok:
                    success_count += 1
                    batch_to_update.append((m_id, rating, imdb_id, genres))

                extra = f"IMDb {rating} ({imdb_id})" if ok else "Not found"
                log_line = tracker.tick(success=ok, title=title, extra=extra)
                try:
                    self.stdout.write(log_line)
                except UnicodeEncodeError:
                    self.stdout.write(log_line.encode('ascii', errors='replace').decode('ascii'))

                # Batch save every 50
                if len(batch_to_update) >= 50:
                    self._save_batch(batch_to_update)
                    batch_to_update = []

        if batch_to_update:
            self._save_batch(batch_to_update)

        self.stdout.write(self.style.SUCCESS(
            f'\n[OK] Finished! Successfully fetched IMDb ratings for {success_count}/{total} movies.'
        ))

    def _save_batch(self, items):
        """Save a batch of IMDb rating and genre updates to Movie and all copies in same group."""
        with transaction.atomic():
            for m_id, rating, imdb_id, genres in items:
                movie = Movie.objects.filter(id=m_id).first()
                if not movie:
                    continue
                # Update this movie and all copies in same group
                update_fields = {'imdb_rating': rating}
                if imdb_id:
                    update_fields['imdb_id'] = imdb_id
                if genres and not movie.genres:
                    update_fields['genres'] = ', '.join(genres)

                if movie.group_key:
                    Movie.objects.filter(group_key=movie.group_key).update(**update_fields)
                else:
                    Movie.objects.filter(id=m_id).update(**update_fields)

                # Also update MovieDetail if exists
                try:
                    detail = MovieDetail.objects.filter(movie_id=m_id).first()
                    if detail:
                        detail.imdb_rating = rating
                        if imdb_id:
                            detail.imdb_id = imdb_id
                        if genres and not detail.genres:
                            detail.genres = genres
                        detail.save()
                except Exception:
                    pass
