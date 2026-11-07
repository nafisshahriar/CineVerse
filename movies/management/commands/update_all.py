from django.core.management.base import BaseCommand
from django.utils import timezone
from movies.models import Movie, MovieDetail
from movies.services import tmdb_service
from movies.utils import ProgressTracker, detect_language
from movies.task_runner import is_task_stopped
from datetime import timedelta
import time


class Command(BaseCommand):
    help = (
        'Unified update pipeline: crawl H5AI sources, fetch TMDB details, '
        'fetch credits, retry failed movies, and refresh metrics — all in one command.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--url', default='', help='H5AI root URL to crawl (skip crawl if empty)')
        parser.add_argument('--force', action='store_true', help='Re-fetch everything regardless of cache')
        parser.add_argument('--skip-crawl', action='store_true', help='Skip crawl step, only update existing movies')
        parser.add_argument('--skip-details', action='store_true', help='Skip TMDB detail/credit fetching')
        parser.add_argument('--refresh-metrics', action='store_true', help='Run TMDB changes refresh as part of pipeline')
        parser.add_argument('--limit', type=int, default=None, help='Max number of TMDB API calls per step')
        parser.add_argument('--delay', type=float, default=0.25, help='Delay between API calls in seconds (rate limit)')

    def handle(self, *args, **options):
        url = options['url']
        force = options['force']
        skip_crawl = options['skip_crawl']
        skip_details = options['skip_details']
        refresh_metrics = options['refresh_metrics']
        limit = options['limit']
        delay = options['delay']

        stats = {
            'crawl_new': 0,
            'crawl_updated': 0,
            'details_fetched': 0,
            'details_skipped': 0,
            'details_failed': 0,
            'credits_fetched': 0,
            'credits_skipped': 0,
            'credits_failed': 0,
            'retried': 0,
            'metrics_refreshed': 0,
        }

        self.stdout.write(self.style.MIGRATE_HEADING('\n🎬 CineVerse Update Pipeline'))
        self.stdout.write(f'  Time: {timezone.now().strftime("%Y-%m-%d %H:%M:%S")}')
        self.stdout.write(f'  Force: {force} | Limit: {limit or "unlimited"} | Delay: {delay}s\n')

        # ── Step 1: Crawl H5AI ──────────────────────────────────────────
        if not skip_crawl and url:
            self.stdout.write(self.style.MIGRATE_HEADING('Step 1/5: Crawling H5AI source'))
            from django.core.management import call_command
            crawl_args = ['--url', url]
            if force:
                crawl_args.append('--force')
            call_command('crawl_h5ai', *crawl_args)
            self.stdout.write(self.style.SUCCESS('  [OK] Crawl complete\n'))
        else:
            if skip_crawl:
                self.stdout.write(self.style.WARNING('Step 1/5: Crawl skipped (--skip-crawl)\n'))
            elif not url:
                self.stdout.write(self.style.WARNING('Step 1/5: Crawl skipped (no --url provided)\n'))

        # ── Step 2: Fetch TMDB details ──────────────────────────────────
        if is_task_stopped():
            self.stdout.write(self.style.WARNING('\n[STOPPED] Pipeline halted by user.\n'))
            return

        if not skip_details:
            self.stdout.write(self.style.MIGRATE_HEADING('Step 2/5: Fetching TMDB details'))
            self._fetch_details(stats, force, limit, delay)
            self.stdout.write(self.style.SUCCESS(
                f'  [OK] Details: {stats["details_fetched"]} fetched, '
                f'{stats["details_skipped"]} skipped, '
                f'{stats["details_failed"]} failed\n'
            ))
        else:
            self.stdout.write(self.style.WARNING('Step 2/5: Details skipped (--skip-details)\n'))

        # ── Step 3: Fetch credits ───────────────────────────────────────
        if is_task_stopped():
            self.stdout.write(self.style.WARNING('\n[STOPPED] Pipeline halted by user.\n'))
            return

        if not skip_details:
            self.stdout.write(self.style.MIGRATE_HEADING('Step 3/5: Fetching cast & crew'))
            self._fetch_credits(stats, force, limit, delay)
            self.stdout.write(self.style.SUCCESS(
                f'  [OK] Credits: {stats["credits_fetched"]} fetched, '
                f'{stats["credits_skipped"]} skipped, '
                f'{stats["credits_failed"]} failed\n'
            ))
        else:
            self.stdout.write(self.style.WARNING('Step 3/5: Credits skipped (--skip-details)\n'))

        # ── Step 4: Retry failed movies ─────────────────────────────────
        self.stdout.write(self.style.MIGRATE_HEADING('Step 4/5: Retrying failed movies'))
        self._retry_failed(stats, delay)
        self.stdout.write(self.style.SUCCESS(f'  [OK] Retried: {stats["retried"]}\n'))

        # ── Step 5: Refresh metrics (optional or on-demand) ─────────────
        if refresh_metrics:
            self.stdout.write(self.style.MIGRATE_HEADING('Step 5/5: Refreshing metrics via TMDB Changes API'))
            from django.core.management import call_command
            call_command('refresh_metadata', '--changes', '--days', '7')
            self.stdout.write(self.style.SUCCESS('  [OK] Metrics refresh complete\n'))
        else:
            self.stdout.write(self.style.NOTICE('Step 5/5: Metrics refresh skipped (use --refresh-metrics to run)\n'))

        # ── Summary ─────────────────────────────────────────────────────
        self._print_summary(stats)

    def _fetch_details(self, stats, force, limit, delay):
        """Fetch MovieDetail for movies that don't have one yet."""
        if force:
            movies = Movie.objects.filter(fetched=True)
        else:
            movies = Movie.objects.filter(fetched=True).exclude(
                detail__tmdb_id__isnull=False
            )

        total = movies.count()
        if limit:
            movies = movies[:limit]
            total = min(total, limit)

        if total == 0:
            self.stdout.write('  No movies needing TMDB details')
            return

        self.stdout.write(f'  Found {total} movies needing details')
        tracker = ProgressTracker(total, desc="TMDB Details")

        for movie in movies:
            search_title = movie.clean_title or movie.title
            try:
                extended = tmdb_service.get_extended_details(
                    search_title, movie.year_str or ''
                )
                if not extended:
                    stats['details_failed'] += 1
                    self.stdout.write(tracker.tick(False, search_title, "No TMDB match"))
                    continue

                detail, _ = MovieDetail.objects.get_or_create(movie=movie)
                detail.tmdb_id = extended.get('tmdb_id')
                detail.overview = extended.get('overview', '')
                detail.genres = extended.get('genres', [])
                detail.runtime = extended.get('runtime')
                detail.backdrop_url = extended.get('backdrop_url')
                detail.tagline = extended.get('tagline', '')
                detail.imdb_id = extended.get('imdb_id', '')
                detail.original_language = extended.get('original_language', '')
                detail.budget = extended.get('budget')
                detail.revenue = extended.get('revenue')
                detail.production_companies = extended.get('production_companies', [])
                detail.cast = extended.get('cast', [])
                detail.director = extended.get('director', {})

                if extended.get('release_date'):
                    try:
                        from datetime import datetime
                        detail.release_date = datetime.strptime(
                            extended['release_date'], '%Y-%m-%d'
                        ).date()
                    except ValueError:
                        pass

                detail.save()

                # Update movie fields if newly learned
                if extended.get('original_language') and not movie.language:
                    movie.language = detect_language(tmdb_code=extended['original_language'])
                    movie.save()

                stats['details_fetched'] += 1
                self.stdout.write(tracker.tick(True, movie.display_title))

            except Exception as e:
                stats['details_failed'] += 1
                self.stdout.write(tracker.tick(False, search_title, str(e)))

            time.sleep(delay)

    def _fetch_credits(self, stats, force, limit, delay):
        """Fetch cast/director for MovieDetails that are missing them."""
        if force:
            details = MovieDetail.objects.filter(tmdb_id__isnull=False)
        else:
            details = MovieDetail.objects.filter(
                tmdb_id__isnull=False, cast=[]
            )

        total = details.count()
        if limit:
            details = details[:limit]
            total = min(total, limit)

        if total == 0:
            self.stdout.write('  No movies needing credits')
            return

        self.stdout.write(f'  Found {total} movies needing credits')
        tracker = ProgressTracker(total, desc="Credits")

        for detail in details:
            try:
                credits_data = tmdb_service._extract_credits(detail.tmdb_id)
                cast = credits_data.get('cast', [])
                director = credits_data.get('director', {})

                if cast or director:
                    detail.cast = cast
                    detail.director = director
                    detail.save()
                    stats['credits_fetched'] += 1
                    dir_name = director.get('name', '?') if director else '?'
                    self.stdout.write(tracker.tick(True, detail.movie.display_title, f"Dir: {dir_name}, Cast: {len(cast)}"))
                else:
                    stats['credits_skipped'] += 1
                    self.stdout.write(tracker.tick(False, detail.movie.display_title, "No credits"))

            except Exception as e:
                stats['credits_failed'] += 1
                self.stdout.write(tracker.tick(False, detail.movie.display_title, str(e)))

            time.sleep(delay)

    def _retry_failed(self, stats, delay):
        """Retry movies whose metadata_status is 'missing'/'failed' and next_crawl has passed."""
        now = timezone.now()
        retryable = Movie.objects.filter(
            metadata_status__in=['missing', 'failed'],
        ).filter(
            next_crawl__lte=now
        ) | Movie.objects.filter(
            metadata_status__in=['missing', 'failed'],
            next_crawl__isnull=True
        )

        retryable = retryable.distinct()[:30]
        count = retryable.count()
        if count == 0:
            self.stdout.write('  No movies eligible for retry')
            return

        self.stdout.write(f'  Found {count} movies to retry')
        tracker = ProgressTracker(count, desc="Retrying")

        for movie in retryable:
            search_title = movie.clean_title or movie.title
            try:
                extended = tmdb_service.get_extended_details(
                    search_title, movie.year_str or ''
                )
                if extended:
                    movie.poster_url = extended.get('poster_url') or movie.poster_url
                    movie.popularity = extended.get('popularity') or movie.popularity
                    movie.vote_count = extended.get('vote_count') or movie.vote_count
                    movie.vote_average = extended.get('vote_average') or movie.vote_average
                    movie.fetched = True
                    movie.metadata_status = 'ok'
                    movie.next_crawl = None
                    movie.last_crawled = now
                    if extended.get('original_language'):
                        movie.language = detect_language(tmdb_code=extended['original_language'])
                    movie.save()
                    stats['retried'] += 1
                    self.stdout.write(tracker.tick(True, movie.display_title, "Recovered OK"))
                else:
                    movie.next_crawl = now + timedelta(hours=12)
                    movie.save()
                    self.stdout.write(tracker.tick(False, movie.display_title, "Still missing"))
            except Exception as e:
                movie.next_crawl = now + timedelta(hours=24)
                movie.save()
                self.stdout.write(tracker.tick(False, movie.display_title, str(e)))

            time.sleep(delay)

    def _print_summary(self, stats):
        total_movies = Movie.objects.count()
        with_details = MovieDetail.objects.filter(tmdb_id__isnull=False).count()
        with_credits = MovieDetail.objects.exclude(cast=[]).count()
        missing = Movie.objects.filter(metadata_status__in=['missing', 'failed']).count()

        self.stdout.write(self.style.MIGRATE_HEADING('\n═══ Update Summary ═══'))
        self.stdout.write(f'  Total movies in database : {total_movies}')
        self.stdout.write(f'  With TMDB details        : {with_details}')
        self.stdout.write(f'  With cast/credits        : {with_credits}')
        self.stdout.write(f'  Missing/failed metadata  : {missing}')
        self.stdout.write('')
        self.stdout.write(f'  This run:')
        self.stdout.write(self.style.SUCCESS(f'    Details fetched  : {stats["details_fetched"]}'))
        self.stdout.write(self.style.SUCCESS(f'    Credits fetched  : {stats["credits_fetched"]}'))
        self.stdout.write(self.style.SUCCESS(f'    Retried          : {stats["retried"]}'))
        if stats['details_failed'] or stats['credits_failed']:
            self.stdout.write(self.style.ERROR(
                f'    Failed           : {stats["details_failed"] + stats["credits_failed"]}'
            ))
        self.stdout.write(self.style.SUCCESS('\n[OK] Pipeline completed!'))
