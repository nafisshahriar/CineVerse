from django.core.management.base import BaseCommand
from django.db import transaction
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import requests
import re
from movies.models import Movie, MovieDetail
from movies.utils import ProgressTracker, clean_title_for_search

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
        parser.add_argument('--workers', type=int, default=16, help='Concurrent worker threads (default: 16)')
        parser.add_argument('--force', action='store_true', help='Re-fetch even if genres already exist')

    def handle(self, *args, **options):
        limit = options['limit']
        fetch_all = options['all']
        workers = options['workers']
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

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nFetching genres for {total} movies ({workers} threads)...'
        ))
        tracker = ProgressTracker(total, desc="Genre Fetch")

        def fetch_movie_genres(m):
            title = m.clean_title or m.title
            found_genres = []
            tmdb_id = None

            # 1. Try TMDB search
            try:
                q = clean_title_for_search(title)
                params = {'api_key': api_key, 'query': q}
                if m.year:
                    params['year'] = m.year
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
            if not found_genres and m.imdb_id and m.imdb_id.startswith('tt'):
                try:
                    r2 = requests.get(f'https://v3-cinemeta.strem.io/meta/movie/{m.imdb_id}.json', timeout=4)
                    if r2.status_code == 200:
                        meta = r2.json().get('meta', {})
                        cinemeta_genres = meta.get('genres', [])
                        for cg in cinemeta_genres:
                            if cg and cg not in found_genres:
                                # Normalize Sci-Fi -> Science Fiction
                                norm = 'Science Fiction' if cg.lower() in ('sci-fi', 'scifi') else cg
                                if norm not in found_genres:
                                    found_genres.append(norm)
                except Exception:
                    pass

            return m.id, found_genres, title, tmdb_id

        success_count = 0
        batch_to_update = []

        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_movie = {executor.submit(fetch_movie_genres, m): m for m in movies}

            for future in as_completed(future_to_movie):
                m_id, genres, title, tmdb_id = future.result()
                ok = len(genres) > 0
                if ok:
                    success_count += 1
                    batch_to_update.append((m_id, genres, tmdb_id))

                extra = ', '.join(genres[:3]) if ok else "No genre found"
                log_line = tracker.tick(success=ok, title=title, extra=extra)
                try:
                    self.stdout.write(log_line)
                except UnicodeEncodeError:
                    self.stdout.write(log_line.encode('ascii', errors='replace').decode('ascii'))

                if len(batch_to_update) >= 40:
                    self._save_batch(batch_to_update)
                    batch_to_update = []

        if batch_to_update:
            self._save_batch(batch_to_update)

        self.stdout.write(self.style.SUCCESS(
            f'\n[OK] Finished! Successfully fetched genres for {success_count}/{total} movies.'
        ))

    def _save_batch(self, items):
        """Save a batch of genre updates to Movie and all copies in same group."""
        with transaction.atomic():
            for m_id, genres, tmdb_id in items:
                movie = Movie.objects.filter(id=m_id).first()
                if not movie:
                    continue

                genre_str = ', '.join(genres)
                if movie.group_key:
                    Movie.objects.filter(group_key=movie.group_key).update(genres=genre_str)
                else:
                    Movie.objects.filter(id=m_id).update(genres=genre_str)

                # Update or create MovieDetail.genres if detail exists
                try:
                    detail = MovieDetail.objects.filter(movie=movie).first()
                    if detail:
                        detail.genres = genres
                        if tmdb_id and not detail.tmdb_id:
                            detail.tmdb_id = tmdb_id
                        detail.save(update_fields=['genres', 'tmdb_id'] if tmdb_id else ['genres'])
                except Exception:
                    pass
