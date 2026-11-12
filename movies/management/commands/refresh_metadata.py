from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone
from datetime import datetime, timedelta
import time
from movies.cache_utils import invalidate_catalog_cache
from movies.models import Movie, MovieDetail
from movies.services import tmdb_service
from movies.utils import ProgressTracker
from movies.task_runner import is_task_stopped, detect_language


class Command(BaseCommand):
    help = (
        'Sustainable TMDB metadata refresh for ratings, popularity, and votes. '
        'Supports TMDB Changes API (--changes) to refresh only movies TMDB updated, '
        'saving 95%+ of API calls.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--changes', action='store_true', help='Use TMDB Changes API (only refresh movies updated on TMDB)')
        parser.add_argument('--days', type=int, default=7, help='Days of TMDB changes to inspect (default: 7)')
        parser.add_argument('--stale', action='store_true', help='Refresh movies with stale metadata (> 14 days old)')
        parser.add_argument('--movie-id', type=int, help='Refresh a specific movie by ID')
        parser.add_argument('--limit', type=int, default=None, help='Limit number of movies to refresh')
        parser.add_argument('--delay', type=float, default=0.2, help='Delay between API calls in seconds (default: 0.2s)')

    def handle(self, *args, **options):
        use_changes = options['changes']
        days = options['days']
        stale_only = options['stale']
        movie_id = options['movie_id']
        limit = options['limit']
        delay = options['delay']

        # Mode 1: Single movie
        if movie_id:
            try:
                movie = Movie.objects.select_related('detail').get(pk=movie_id)
                ok, _ = self._refresh_movie(movie)
                if ok:
                    invalidate_catalog_cache()
            except Movie.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'Movie ID {movie_id} not found'))
            return

        # Mode 2: TMDB Changes API (Sustainable)
        if use_changes:
            movies_to_refresh = self._get_movies_from_changes(days, limit)
        # Mode 3: Stale movies
        elif stale_only:
            cutoff = timezone.now() - timedelta(days=14)
            movies_to_refresh = Movie.objects.select_related('detail').filter(
                fetched=True,
                detail__tmdb_id__isnull=False,
                last_crawled__lt=cutoff
            ).order_by('-popularity')
            if limit:
                movies_to_refresh = list(movies_to_refresh[:limit])
            else:
                movies_to_refresh = list(movies_to_refresh)
        # Mode 4: All fetched movies
        else:
            movies_to_refresh = Movie.objects.select_related('detail').filter(
                fetched=True,
                detail__tmdb_id__isnull=False
            ).order_by('-popularity')
            if limit:
                movies_to_refresh = list(movies_to_refresh[:limit])
            else:
                movies_to_refresh = list(movies_to_refresh)

        total = len(movies_to_refresh)
        if total == 0:
            self.stdout.write(self.style.SUCCESS('\n[OK] No movies need refreshing right now.'))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f'\nRefreshing metrics for {total} movies (delay: {delay}s)...'))
        tracker = ProgressTracker(total, desc="Refreshing")

        success_count = 0
        rating_shifts = 0

        for movie in movies_to_refresh:
            if is_task_stopped():
                self.stdout.write(self.style.WARNING("\n[STOPPED] Metadata refresh halted by user."))
                break
            ok, shift_str = self._refresh_movie(movie)
            if ok:
                success_count += 1
                if shift_str:
                    rating_shifts += 1
            log_line = tracker.tick(success=ok, title=movie.display_title, extra=shift_str)
            self.stdout.write(log_line)
            time.sleep(delay)

        if success_count:
            invalidate_catalog_cache()

        self.stdout.write(self.style.SUCCESS(
            f'\n[OK] Refresh complete! Updated: {success_count}/{total} movies ({rating_shifts} rating changes detected).'
        ))

    def _get_movies_from_changes(self, days: int, limit: int = None) -> list:
        """Fetch changed movie IDs from TMDB and match against our database."""
        end_date = datetime.now().strftime('%Y-%m-%d')
        start_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        self.stdout.write(f'Checking TMDB Changes API from {start_date} to {end_date}...')

        changed_tmdb_ids = set()
        page = 1
        max_pages = 10  # Up to 1,000 changed TMDB movies

        while page <= max_pages:
            res = tmdb_service.get_movie_changes(start_date=start_date, end_date=end_date, page=page)
            if not res or not res.get('results'):
                break
            for item in res['results']:
                changed_tmdb_ids.add(item['id'])
            if page >= res.get('total_pages', 1):
                break
            page += 1

        self.stdout.write(f'  Found {len(changed_tmdb_ids)} changed movies on TMDB in last {days} days')
        matched = Movie.objects.select_related('detail').filter(detail__tmdb_id__in=changed_tmdb_ids).distinct()
        if limit:
            matched = matched[:limit]
        matched_list = list(matched)
        self.stdout.write(f'  Matched {len(matched_list)} movies in our collection')
        return matched_list

    def _refresh_movie(self, movie: Movie) -> tuple[bool, str]:
        """Fetch fresh metrics from TMDB and save."""
        try:
            detail = getattr(movie, 'detail', None)
            if not detail or not detail.tmdb_id:
                return False, 'No TMDB ID'

            data = tmdb_service.get_movie_details(detail.tmdb_id)
            if not data:
                return False, 'No data from TMDB'

            old_rating = movie.vote_average
            old_votes = movie.vote_count
            new_rating = data.get('vote_average') or movie.vote_average
            new_votes = data.get('vote_count') or movie.vote_count
            new_pop = data.get('popularity') or movie.popularity

            shift_str = ''
            if old_rating and new_rating and abs(new_rating - old_rating) >= 0.05:
                shift_str = f"rating: {old_rating:.1f}->{new_rating:.1f}"

            movie.vote_average = new_rating
            movie.vote_count = new_votes
            movie.popularity = new_pop
            movie.last_crawled = timezone.now()

            # Also update language on movie if missing
            if not movie.language and data.get('original_language'):
                movie.language = detect_language(tmdb_code=data['original_language'])

            close_old_connections()
            movie.save()
            return True, shift_str
        except Exception as e:
            return False, str(e)
