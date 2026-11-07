from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import F
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import os
import requests
import time
from movies.models import Movie, MovieDetail
from movies.utils import fetch_imdb_rating_and_id, ProgressTracker, clean_title_for_search
from movies.task_runner import is_task_stopped


class Command(BaseCommand):
    help = 'Fetch authentic IMDb ratings and IMDb IDs (tt...) for movies in the database.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=500, help='Max movies to process (default: 500)')
        parser.add_argument('--all', action='store_true', help='Process all movies missing IMDb ratings')
        parser.add_argument('--workers', type=int, default=4, help='Concurrent worker threads (default: 4)')
        parser.add_argument('--force', action='store_true', help='Re-fetch even if already has IMDb rating')

    def handle(self, *args, **options):
        limit = options['limit']
        fetch_all = options['all']
        workers = options['workers'] or 4
        force = options['force']
        api_key = os.getenv('TMDB_API_KEY')

        qs = Movie.objects.filter(is_primary_copy=True).select_related('detail')
        if not force:
            qs = qs.filter(imdb_rating__isnull=True)

        # Prioritize released movies with real votes/popularity first
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

        # Pre-extract lightweight plain Python dicts to completely isolate worker threads from the ORM
        movie_items = []
        for m in movies:
            known_id = m.imdb_id or ''
            tmdb_id = None
            try:
                if hasattr(m, 'detail') and m.detail:
                    if not known_id and m.detail.imdb_id:
                        known_id = m.detail.imdb_id
                    tmdb_id = m.detail.tmdb_id
            except Exception:
                pass

            movie_items.append({
                'id': m.id,
                'title': m.clean_title or m.title,
                'display_title': m.display_title,
                'year': m.year,
                'genres': m.genres,
                'known_imdb_id': known_id,
                'tmdb_id': tmdb_id,
                'group_key': m.group_key,
            })

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nFetching authentic IMDb ratings for {total} movies ({workers} threads)...'
        ))
        tracker = ProgressTracker(total, desc="IMDb Ratings")

        def process_one_io(item: dict) -> dict:
            """Pure HTTP network worker. Zero database reads/writes in worker threads."""
            if is_task_stopped():
                return {'id': item['id'], 'cancelled': True, 'title': item['display_title']}

            title = item['title']
            year = item['year']
            known_id = item['known_imdb_id']
            tmdb_id = item['tmdb_id']

            # If no known IMDb ID but movie has TMDB ID, query TMDB external IDs
            if not known_id and api_key and tmdb_id:
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

            # If still no known ID, try quick TMDB search to find the canonical IMDb ID
            if not known_id and api_key:
                try:
                    params = {'api_key': api_key, 'query': clean_title_for_search(title)}
                    if year:
                        params['year'] = str(year)
                    r_s = requests.get('https://api.themoviedb.org/3/search/movie', params=params, timeout=5)
                    if r_s.status_code == 200:
                        results = r_s.json().get('results', [])
                        if results:
                            m_id = results[0].get('id')
                            if m_id:
                                r_ext = requests.get(
                                    f"https://api.themoviedb.org/3/movie/{m_id}/external_ids",
                                    params={'api_key': api_key},
                                    timeout=5
                                )
                                if r_ext.status_code == 200:
                                    ext_id = r_ext.json().get('imdb_id')
                                    if ext_id and ext_id.startswith('tt'):
                                        known_id = ext_id
                except Exception:
                    pass

            res = fetch_imdb_rating_and_id(title, year, known_imdb_id=known_id)
            rating = res[0]
            imdb_id = res[1] if len(res) > 1 else None
            genres = res[2] if len(res) > 2 else []

            return {
                'id': item['id'],
                'group_key': item['group_key'],
                'rating': rating,
                'imdb_id': imdb_id,
                'genres': genres,
                'title': item['display_title'],
                'existing_genres': item['genres'],
                'cancelled': False,
            }

        success_count = 0
        batch_to_update = []
        stopped_early = False

        executor = ThreadPoolExecutor(max_workers=workers)
        pending = {executor.submit(process_one_io, item): item for item in movie_items}

        try:
            while pending:
                if is_task_stopped():
                    stopped_early = True
                    self.stdout.write(self.style.WARNING(
                        "\n[STOPPED] Cancellation requested. Halting remaining worker threads..."
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

                    rating = res.get('rating')
                    imdb_id = res.get('imdb_id')
                    title = res.get('title', 'Unknown')
                    ok = rating is not None

                    if ok:
                        success_count += 1
                        batch_to_update.append(res)

                    extra = f"IMDb {rating} ({imdb_id})" if ok else "Not found"
                    log_line = tracker.tick(success=ok, title=title, extra=extra)
                    try:
                        self.stdout.write(log_line)
                    except UnicodeEncodeError:
                        self.stdout.write(log_line.encode('ascii', errors='replace').decode('ascii'))

                    # Atomic batch save strictly on the main thread every 20 items
                    if len(batch_to_update) >= 20:
                        self._save_batch(batch_to_update)
                        batch_to_update = []
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)

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

    def _save_batch(self, items: list):
        """Save a batch of IMDb rating updates strictly on the main thread."""
        with transaction.atomic():
            for item in items:
                m_id = item['id']
                rating = item.get('rating')
                imdb_id = item.get('imdb_id')
                genres = item.get('genres') or []
                existing_genres = item.get('existing_genres')
                group_key = item.get('group_key')

                if rating is None:
                    continue

                sync_data = {'imdb_rating': rating}
                if imdb_id:
                    sync_data['imdb_id'] = imdb_id
                if genres and not existing_genres:
                    sync_data['genres'] = ', '.join(genres)

                if group_key:
                    Movie.objects.filter(group_key=group_key).update(**sync_data)
                else:
                    Movie.objects.filter(id=m_id).update(**sync_data)

                # Update detail model if present
                try:
                    detail = MovieDetail.objects.filter(movie_id=m_id).first()
                    if detail:
                        d_updated = False
                        if rating and detail.imdb_rating != rating:
                            detail.imdb_rating = rating
                            d_updated = True
                        if imdb_id and not detail.imdb_id:
                            detail.imdb_id = imdb_id
                            d_updated = True
                        if genres and not detail.genres:
                            detail.genres = genres
                            d_updated = True
                        if d_updated:
                            detail.save()
                except Exception:
                    pass
