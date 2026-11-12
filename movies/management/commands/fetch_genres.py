from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import os
import requests
import re
from movies.cache_utils import invalidate_catalog_cache
from movies.models import Movie, MovieDetail
from movies.utils import ProgressTracker, clean_title_for_search
from movies.task_runner import is_task_stopped

TMDB_GENRES = {
    28: 'Action',
    12: 'Adventure',
    16: 'Animation',
    35: 'Comedy',
    80: 'Crime',
    99: 'Documentary',
    18: 'Drama',
    10751: 'Family',
    14: 'Fantasy',
    36: 'History',
    27: 'Horror',
    10402: 'Music',
    9648: 'Mystery',
    10749: 'Romance',
    878: 'Science Fiction',
    10770: 'TV Movie',
    53: 'Thriller',
    10752: 'War',
    37: 'Western',
}


class Command(BaseCommand):
    help = 'Fast multi-threaded genre fetcher using TMDB and Cinemeta APIs.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500, help='Max movies to process (default: 500)')
        parser.add_argument('--all', action='store_true', help='Process all movies missing genres')
        parser.add_argument('--workers', type=int, default=4, help='Concurrent worker threads (default: 4)')
        parser.add_argument('--force', action='store_true', help='Re-fetch even if genres already exist')

    def handle(self, *args, **options):
        limit = options['limit']
        fetch_all = options['all']
        workers = options['workers'] or 4
        force = options['force']

        api_key = os.getenv('TMDB_API_KEY')
        if not api_key:
            self.stdout.write(self.style.ERROR('[FAIL] TMDB_API_KEY environment variable is not set!'))
            return

        qs = Movie.objects.filter(is_primary_copy=True)
        if not force:
            qs = qs.filter(genres='')

        # Prioritize popular and higher vote movies
        qs = qs.order_by('-popularity', '-vote_count', '-year')

        if not fetch_all and limit:
            movies = list(qs[:limit])
        else:
            movies = list(qs)

        total = len(movies)
        if total == 0:
            self.stdout.write(self.style.SUCCESS('\n[OK] All targeted movies already have genres!'))
            return

        # Pre-extract lightweight plain Python dicts to isolate threads from ORM
        movie_items = [
            {
                'id': m.id,
                'title': m.clean_title or m.title,
                'display_title': m.display_title,
                'year': m.year,
                'imdb_id': m.imdb_id,
                'group_key': m.group_key,
            }
            for m in movies
        ]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nFetching genres for {total} movies ({workers} threads)...'
        ))
        tracker = ProgressTracker(total, desc="Genre Fetch")

        def fetch_movie_genres_io(item: dict) -> dict:
            """Pure HTTP network worker. Zero database reads/writes in worker threads."""
            if is_task_stopped():
                return {'id': item['id'], 'cancelled': True, 'title': item['display_title']}

            title = item['title']
            year = item['year']
            imdb_id = item['imdb_id']
            found_genres = []
            tmdb_id = None

            # 1. Try TMDB search
            try:
                q = clean_title_for_search(title)
                params = {'api_key': api_key, 'query': q}
                if year:
                    params['year'] = year
                r = requests.get('https://api.themoviedb.org/3/search/movie', params=params, timeout=5)
                if r.status_code == 200:
                    data = r.json()
                    results = data.get('results') or []
                    if results:
                        best = results[0]
                        tmdb_id = best.get('id')
                        g_ids = best.get('genre_ids', [])
                        for gid in g_ids:
                            if gid in TMDB_GENRES and TMDB_GENRES[gid] not in found_genres:
                                found_genres.append(TMDB_GENRES[gid])
            except Exception:
                pass

            # 2. If no genres from TMDB, try Cinemeta via IMDb ID
            if not found_genres and imdb_id and imdb_id.startswith('tt'):
                try:
                    r2 = requests.get(f'https://v3-cinemeta.strem.io/meta/movie/{imdb_id}.json', timeout=4)
                    if r2.status_code == 200:
                        meta = r2.json().get('meta', {})
                        cinemeta_genres = meta.get('genres', [])
                        for cg in cinemeta_genres:
                            if cg and cg not in found_genres:
                                norm = 'Science Fiction' if cg.lower() in ('sci-fi', 'scifi') else cg
                                if norm not in found_genres:
                                    found_genres.append(norm)
                except Exception:
                    pass

            return {
                'id': item['id'],
                'group_key': item['group_key'],
                'genres': found_genres,
                'tmdb_id': tmdb_id,
                'title': item['display_title'],
                'cancelled': False,
            }

        success_count = 0
        batch_to_update = []
        stopped_early = False

        executor = ThreadPoolExecutor(max_workers=workers)
        pending = {executor.submit(fetch_movie_genres_io, item): item for item in movie_items}

        try:
            while pending:
                if is_task_stopped():
                    stopped_early = True
                    self.stdout.write(self.style.WARNING(
                        "\n[STOPPED] Cancellation requested. Halting all worker threads..."
                    ))
                    for f in pending:
                        f.cancel()
                    break

                done, _ = concurrent.futures.wait(
                    pending.keys(),
                    timeout=0.5,
                    return_when=concurrent.futures.FIRST_COMPLETED
                )

                for future in done:
                    pending.pop(future, None)
                    try:
                        res = future.result()
                    except Exception:
                        continue

                    if res.get('cancelled'):
                        continue

                    genres = res.get('genres') or []
                    title = res.get('title', 'Unknown')
                    ok = len(genres) > 0
                    if ok:
                        success_count += 1
                        batch_to_update.append(res)

                    extra = ', '.join(genres[:3]) if ok else "No genre found"
                    log_line = tracker.tick(success=ok, title=title, extra=extra)
                    try:
                        self.stdout.write(log_line)
                    except UnicodeEncodeError:
                        self.stdout.write(log_line.encode('ascii', errors='replace').decode('ascii'))

                    if len(batch_to_update) >= 25:
                        self._save_batch(batch_to_update)
                        batch_to_update = []
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)

        if batch_to_update:
            self._save_batch(batch_to_update)

        if success_count:
            invalidate_catalog_cache()

        if stopped_early:
            self.stdout.write(self.style.WARNING(
                f'\n[STOPPED] Task halted by user. Successfully fetched genres for {success_count} movies before stopping.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'\n[OK] Finished! Successfully fetched genres for {success_count}/{total} movies.'
            ))

    def _save_batch(self, items: list):
        """Save a batch of genre updates strictly on the main thread."""
        close_old_connections()
        movie_ids = [item['id'] for item in items if item.get('genres')]
        existing_details = (
            {d.movie_id: d for d in MovieDetail.objects.filter(movie_id__in=movie_ids)}
            if movie_ids else {}
        )
        details_to_update = []
        with transaction.atomic():
            for item in items:
                m_id = item['id']
                genres = item.get('genres') or []
                if not genres:
                    continue
                tmdb_id = item.get('tmdb_id')
                group_key = item.get('group_key')
                genre_str = ', '.join(genres)

                if group_key:
                    Movie.objects.filter(group_key=group_key).update(genres=genre_str)
                else:
                    Movie.objects.filter(id=m_id).update(genres=genre_str)

                detail = existing_details.get(m_id)
                if detail:
                    detail.genres = genres
                    if tmdb_id and not detail.tmdb_id:
                        detail.tmdb_id = tmdb_id
                    details_to_update.append(detail)

            if details_to_update:
                try:
                    MovieDetail.objects.bulk_update(details_to_update, ['genres', 'tmdb_id'])
                except Exception:
                    pass
