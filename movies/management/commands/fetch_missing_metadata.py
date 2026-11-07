from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import time
from movies.models import Movie, MovieDetail
from movies.services import tmdb_service
from movies.utils import ProgressTracker, clean_title_for_search, TMDB_GENRES
from movies.task_runner import is_task_stopped


class Command(BaseCommand):
    help = 'Concurrently fetch missing posters, overviews, ratings, and TMDB metadata.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=None, help='Max movies to process (default: all missing)')
        parser.add_argument('--workers', type=int, default=12, help='Concurrent worker threads (default: 12)')
        parser.add_argument('--force', action='store_true', help='Re-fetch even if movie already has a poster')
        parser.add_argument('--include-details', action='store_true', help='Fetch extended details (cast, director, runtime, backdrop)')
        parser.add_argument('--all', action='store_true', help='Process all movies missing posters')

    def handle(self, *args, **options):
        limit = options['limit']
        workers = options['workers']
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

        self.stdout.write(self.style.MIGRATE_HEADING(
            f'\nFetching missing posters & metadata for {total} movies using {workers} concurrent threads...'
        ))
        tracker = ProgressTracker(total, desc="Posters & Meta")

        matched_count = 0
        failed_count = 0
        stopped_early = False

        def process_movie(movie: Movie):
            if is_task_stopped():
                return False, movie.display_title, 'Cancelled', True

            clean_t = movie.clean_title or movie.title
            year_str = str(movie.year) if movie.year else ''

            # Robust multi-pass TMDB lookup (with/without year, track stripping, symbol stripping)
            res = tmdb_service.search_movie_multipass(clean_t, year_str)
            if not res and movie.title and movie.title != clean_t:
                res = tmdb_service.search_movie_multipass(movie.title, '')

            if not res:
                movie.metadata_status = 'failed'
                movie.save(update_fields=['metadata_status'])
                return False, movie.display_title, 'Not found on TMDB', False

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
            genre_str = ', '.join([g for g in genre_names if g]) if genre_names else movie.genres

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
            if genre_str and not movie.genres:
                update_data['genres'] = genre_str

            with transaction.atomic():
                for k, v in update_data.items():
                    setattr(movie, k, v)
                movie.save()

                # Sync across all server copies sharing the same group key
                if movie.group_key:
                    Movie.objects.filter(group_key=movie.group_key).update(**update_data)

                # Extended details if requested
                if include_details and tmdb_id:
                    try:
                        ext = tmdb_service.get_extended_details(clean_t, year_str)
                        if ext:
                            detail, _ = MovieDetail.objects.get_or_create(movie=movie)
                            detail.tmdb_id = tmdb_id
                            detail.overview = ext.get('overview', '')
                            detail.genres = ext.get('genres', [])
                            detail.runtime = ext.get('runtime')
                            detail.backdrop_url = ext.get('backdrop_url') or backdrop_url
                            detail.tagline = ext.get('tagline', '')
                            detail.cast = ext.get('cast', [])
                            detail.director = ext.get('director', {})
                            detail.save()
                    except Exception:
                        pass

            has_poster_str = 'Poster found' if poster_url else 'No poster image'
            return True, movie.display_title, has_poster_str, False

        # Execute concurrent tasks
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_movie = {executor.submit(process_movie, m): m for m in movies}

            for future in as_completed(future_to_movie):
                if is_task_stopped():
                    stopped_early = True
                    self.stdout.write(self.style.WARNING(
                        "\n[STOPPED] Cancellation requested. Halting all worker threads..."
                    ))
                    for f in future_to_movie:
                        f.cancel()
                    executor.shutdown(wait=False, cancel_futures=True)
                    break

                try:
                    res = future.result()
                    if len(res) >= 4 and res[3]:  # cancelled mid-flight
                        continue
                    success, title, extra = res[:3]
                    if success:
                        matched_count += 1
                    else:
                        failed_count += 1
                    log_line = tracker.tick(success=success, title=title, extra=extra)
                    try:
                        self.stdout.write(log_line)
                    except UnicodeEncodeError:
                        self.stdout.write(log_line.encode('ascii', errors='replace').decode('ascii'))
                except Exception as e:
                    failed_count += 1
                    self.stdout.write(self.style.ERROR(f"Error processing movie: {e}"))

        if stopped_early:
            self.stdout.write(self.style.WARNING(
                f'\n[STOPPED] Task halted by user. Enriched: {matched_count} movies before stopping.'
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f'\n[OK] Metadata fetch complete! Matched & enriched: {matched_count}/{total} movies ({failed_count} not found).'
            ))
