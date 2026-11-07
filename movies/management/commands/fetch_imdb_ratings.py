from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import requests
import time
from movies.models import Movie, MovieDetail
from movies.utils import fetch_imdb_rating_and_id, ProgressTracker
from movies.task_runner import is_task_stopped


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
        api_key = os.getenv('TMDB_API_KEY')

        qs = Movie.objects.filter(is_primary_copy=True)
        if not force:
            qs = qs.filter(imdb_rating__isnull=True)

        # Prioritize released movies with real votes/popularity first
        # This prevents unreleased 2026/2025 teaser folders from clogging the front of the queue
        qs = qs.order_by(
            F('vote_count').desc(nulls_last=True),
            F('popularity').desc(nulls_last=True),
            F('year').desc(nulls_last=True)
        )

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
            if is_task_stopped():
                return m.id, None, None, [], m.clean_title or m.title, True

            known_id = m.imdb_id or ''
            try:
                if hasattr(m, 'detail') and m.detail and m.detail.imdb_id:
                    known_id = m.detail.imdb_id
            except Exception:
                pass

            title = m.clean_title or m.title

            # If no known IMDb ID but movie has TMDB ID, query TMDB external IDs
            if not known_id and api_key:
                tmdb_id = None
                try:
                    if hasattr(m, 'detail') and m.detail and m.detail.tmdb_id:
                        tmdb_id = m.detail.tmdb_id
                except Exception:
                    pass

                if tmdb_id:
                    try:
                        r = requests.get(
                            f"https://api.themoviedb.org/3/movie/{tmdb_id}/external_ids",
                            params={'api_key': api_key},
                            timeout=5
                        )
                        if r.status_code == 200:
                            ext_id = r.json().get('imdb_id')
                            if ext_id and ext_id.startswith('tt'):
                                known_id = ext_id
                    except Exception:
                        pass

            res = fetch_imdb_rating_and_id(title, m.year, known_imdb_id=known_id)
            rating = res[0]
            imdb_id = res[1] if len(res) > 1 else None
            genres = res[2] if len(res) > 2 else []
            return m.id, rating, imdb_id, genres, title, False

        success_count = 0
        batch_to_update = []
        stopped_early = False

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_movie = {executor.submit(process_one, m): m for m in movies}

            for future in as_completed(future_to_movie):
                if is_task_stopped():
                    stopped_early = True
                    self.stdout.write(self.style.WARNING(
                        "\n[STOPPED] Cancellation requested. Halting remaining worker threads..."
                    ))
                    for f in future_to_movie:
                        f.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                try:
                    res = future.result()
                    if len(res) >= 6 and res[5]:  # Task was stopped mid-execution
                        continue
                    m_id, rating, imdb_id, genres, title = res[:5]
                except Exception:
                    continue

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

                # Batch save every 25
                if len(batch_to_update) >= 25:
                    self._save_batch(batch_to_update)
                    batch_to_update = []

        if batch_to_update:
            self._save_batch(batch_to_update)

        if stopped_early:
            self.stdout.write(self.style.WARNING(
                f'\n[STOPPED] Task halted by user. Successfully fetched IMDb ratings for {success_count} movies before stopping.'
            ))
        else:
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

                movie.imdb_rating = rating
                if imdb_id:
                    movie.imdb_id = imdb_id
                if genres and not movie.genres:
                    movie.genres = ', '.join(genres)
                movie.save(update_fields=['imdb_rating', 'imdb_id', 'genres'])

                # Update detail model if present
                if hasattr(movie, 'detail') and movie.detail:
                    d = movie.detail
                    if imdb_id and not d.imdb_id:
                        d.imdb_id = imdb_id
                    if genres and not d.genres:
                        d.genres = genres
                    d.save()

                # Sync to all server copies sharing the same group key
                if movie.group_key:
                    sync_data = {'imdb_rating': rating}
                    if imdb_id:
                        sync_data['imdb_id'] = imdb_id
                    if genres and not movie.genres:
                        sync_data['genres'] = ', '.join(genres)
                    Movie.objects.filter(group_key=movie.group_key).exclude(id=movie.id).update(**sync_data)
