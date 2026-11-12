from django.core.management.base import BaseCommand
from django.db import close_old_connections, transaction
from django.db.models import Q
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import os
import re
import time
from movies.cache_utils import invalidate_catalog_cache
from movies.models import Movie, MovieDetail
from movies.services import tmdb_service
from movies.utils import ProgressTracker, clean_title_for_search, TMDB_GENRES
from movies.task_runner import is_task_stopped


class Command(BaseCommand):
    help = 'Concurrently fetch missing posters, overviews, ratings, and TMDB metadata.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None, help='Max movies to process (default: all missing)')
        parser.add_argument('--workers', type=int, default=4, help='Concurrent worker threads (default: 4)')
        parser.add_argument('--force', action='store_true', help='Re-fetch even if movie already has a poster')
        parser.add_argument('--include-details', action='store_true', help='Fetch extended details (cast, director, runtime, backdrop)')
        parser.add_argument('--all', action='store_true', help='Process all movies missing posters')

    def handle(self, *args, **options):
        limit = options['limit']
        workers = options['workers'] or 4
        force = options['force']
        include_details = options['include_details']

        api_key = os.getenv('TMDB_API_KEY')
        if not api_key:
            self.stdout.write(self.style.ERROR('[FAIL] TMDB_API_KEY environment variable is not set!'))
            return

        qs = Movie.objects.filter(is_primary_copy=True)
        if not force:
            qs = qs.filter(Q(poster_url__isnull=True) | Q(poster_url='') | Q(metadata_status='missing'))

        # Process movies ordered by id desc
        qs = qs.order_by('-id')

        if limit:
            movies = list(qs[:limit])
        else:
            movies = list(qs)

        total = len(movies)
        if total == 0:
            self.stdout.write(self.style.SUCCESS('\n[OK] All movies already have posters and metadata populated!'))
            return

        # Pre-extract lightweight plain Python dictionaries to avoid ORM access across threads
        movie_items = [
            {
                'id': m.id,
                'title': m.title,
                'clean_title': m.clean_title or m.title,
                'display_title': m.display_title,
                'year': m.year,
                'genres': m.genres,
                'imdb_id': m.imdb_id,
                'group_key': m.group_key,
            }
            for m in movies
        ]

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nFetching missing posters & metadata for {total} movies using {workers} concurrent threads...'
        ))
        tracker = ProgressTracker(total, desc="Posters & Meta")

        matched_count = 0
        failed_count = 0
        stopped_early = False

        def process_movie_io(item: dict) -> dict:
            """Pure network I/O worker. Zero database reads/writes in worker threads."""
            if is_task_stopped():
                return {'id': item['id'], 'cancelled': True, 'title': item['display_title']}

            clean_t = item['clean_title']
            year_str = str(item['year']) if item['year'] else ''

            from movies.utils import normalize_imdb_id, fetch_cinemeta_details
            clean_imdb = normalize_imdb_id(item.get('imdb_id'))

            res = None
            if clean_imdb:
                res = tmdb_service.find_by_imdb_id(clean_imdb)

            # Robust multi-pass TMDB lookup (with/without year, track stripping, symbol stripping)
            if not res:
                res = tmdb_service.search_movie_multipass(clean_t, year_str)
            if not res and item['title'] and item['title'] != clean_t:
                res = tmdb_service.search_movie_multipass(item['title'], '')

            if not res and clean_imdb:
                cm = fetch_cinemeta_details(clean_imdb)
                if cm:
                    update_data = {
                        'metadata_status': 'ok',
                        'poster_url': cm.get('poster_url'),
                        'genres': cm.get('genres_str') or item['genres'],
                    }
                    if cm.get('imdb_rating') is not None:
                        update_data['imdb_rating'] = cm['imdb_rating']
                    if cm.get('runtime'):
                        update_data['runtime'] = cm['runtime']
                    if cm.get('title') and cm['title'] != item['title']:
                        update_data['clean_title'] = cm['title']
                    if cm.get('year'):
                        update_data['year'] = cm['year']
                        update_data['year_str'] = str(cm['year'])

                    detail_data = None
                    if include_details:
                        detail_data = {
                            'tmdb_id': cm.get('moviedb_id'),
                            'overview': cm.get('overview', ''),
                            'genres': cm.get('genres', []),
                            'runtime': cm.get('runtime'),
                            'backdrop_url': cm.get('backdrop_url'),
                            'tagline': '',
                            'imdb_id': clean_imdb,
                            'imdb_rating': cm.get('imdb_rating'),
                            'original_language': '',
                            'cast': cm.get('cast', []),
                            'director': cm.get('director', {}),
                            'trailer_key': cm.get('trailer_key'),
                            'trailer_url': cm.get('trailer_url'),
                        }
                    return {
                        'id': item['id'],
                        'success': True,
                        'title': cm.get('title') or item['display_title'],
                        'update_data': update_data,
                        'detail_data': detail_data,
                        'group_key': item['group_key'],
                        'cancelled': False,
                    }

            if not res:
                return {
                    'id': item['id'],
                    'success': False,
                    'title': item['display_title'],
                    'extra': 'Not found on TMDB',
                    'cancelled': False,
                }

            # Extract fields
            poster_path = res.get('poster_path')
            poster_url = tmdb_service.poster_url(poster_path) if poster_path else None
            backdrop_path = res.get('backdrop_path')
            backdrop_url = tmdb_service.backdrop_url(backdrop_path) if backdrop_path else None

            popularity = res.get('popularity')
            vote_avg = res.get('vote_average')
            vote_cnt = res.get('vote_count')
            tmdb_id = res.get('id')

            # Resolve genres if missing
            genre_names = []
            if res.get('genre_ids'):
                genre_names = [TMDB_GENRES.get(gid) for gid in res['genre_ids'] if gid in TMDB_GENRES]
            genre_str = ', '.join([g for g in genre_names if g]) if genre_names else item['genres']

            update_data = {
                'metadata_status': 'ok',
            }
            if poster_url:
                update_data['poster_url'] = poster_url
            if popularity is not None:
                update_data['popularity'] = popularity
            if vote_avg is not None:
                update_data['vote_average'] = vote_avg
            if vote_cnt is not None:
                update_data['vote_count'] = vote_cnt
            if genre_str and not item['genres']:
                update_data['genres'] = genre_str

            detail_data = None
            if include_details and tmdb_id:
                try:
                    ext = tmdb_service.get_extended_details(clean_t, year_str, imdb_id=clean_imdb or '')
                    if ext:
                        detail_data = {
                            'tmdb_id': tmdb_id,
                            'overview': ext.get('overview', ''),
                            'genres': ext.get('genres', []),
                            'runtime': ext.get('runtime'),
                            'backdrop_url': ext.get('backdrop_url') or backdrop_url,
                            'tagline': ext.get('tagline', ''),
                            'cast': ext.get('cast', []),
                            'director': ext.get('director', {}),
                        }
                except Exception:
                    pass

            has_poster_str = 'Poster found' if poster_url else 'No poster image'
            return {
                'id': item['id'],
                'group_key': item['group_key'],
                'success': True,
                'update_data': update_data,
                'detail_data': detail_data,
                'title': item['display_title'],
                'extra': has_poster_str,
                'cancelled': False,
            }

        # Concurrency loop: non-blocking wait with frequent cancellation checks
        executor = ThreadPoolExecutor(max_workers=workers)
        pending = {executor.submit(process_movie_io, item): item for item in movie_items}
        batch_to_save = []

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
                    except Exception as e:
                        failed_count += 1
                        continue

                    if res.get('cancelled'):
                        continue

                    success = res.get('success', False)
                    title = res.get('title', 'Unknown')
                    extra = res.get('extra', '')
                    if success:
                        matched_count += 1
                    else:
                        failed_count += 1

                    batch_to_save.append(res)

                    log_line = tracker.tick(success=success, title=title, extra=extra)
                    try:
                        self.stdout.write(log_line)
                    except UnicodeEncodeError:
                        self.stdout.write(log_line.encode('ascii', errors='replace').decode('ascii'))

                    # Batch write strictly on the single main thread
                    if len(batch_to_save) >= 20:
                        self._save_batch(batch_to_save)
                        batch_to_save = []
        finally:
            try:
                executor.shutdown(wait=False, cancel_futures=True)
            except TypeError:
                executor.shutdown(wait=False)

        if batch_to_save:
            self._save_batch(batch_to_save)

        if matched_count:
            invalidate_catalog_cache()

        if stopped_early:
            self.stdout.write(self.style.WARNING(
                f'\n[STOPPED] Task halted by user. Enriched: {matched_count} movies before stopping.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'\n[OK] Metadata fetch complete! Matched & enriched: {matched_count}/{total} movies ({failed_count} not found).'
            ))

    def _save_batch(self, items: list):
        """Perform atomic batch database writes strictly on the main thread."""
        close_old_connections()
        detail_movie_ids = [item['id'] for item in items if item.get('success') and item.get('detail_data')]
        existing_details = (
            {d.movie_id: d for d in MovieDetail.objects.filter(movie_id__in=detail_movie_ids)}
            if detail_movie_ids else {}
        )
        with transaction.atomic():
            for item in items:
                m_id = item['id']
                if not item.get('success'):
                    Movie.objects.filter(id=m_id).update(metadata_status='failed')
                    continue

                update_data = item.get('update_data', {})
                group_key = item.get('group_key')
                if group_key:
                    Movie.objects.filter(group_key=group_key).update(**update_data)
                else:
                    Movie.objects.filter(id=m_id).update(**update_data)

                detail_data = item.get('detail_data')
                if detail_data:
                    try:
                        detail = existing_details.get(m_id)
                        if detail is None:
                            detail = MovieDetail(movie_id=m_id, **detail_data)
                        else:
                            for k, v in detail_data.items():
                                setattr(detail, k, v)
                        detail.save()
                    except Exception:
                        pass
