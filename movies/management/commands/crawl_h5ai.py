from django.core.management.base import BaseCommand
from django.db import IntegrityError
from django.utils import timezone
from movies.models import Movie, CrawledDirectory, FailedParse
from movies.utils import (
    crawl_h5ai_recursive, parse_title_year, extract_last_modified_from_text,
    find_media_in_directory, MEDIA_EXTS, list_h5ai_directory
)
from movies.services import fetch_poster_and_details
from datetime import timedelta
import requests


class Command(BaseCommand):
    help = 'Crawl an H5AI root URL and import movies (timestamp-aware crawler)'

    def add_arguments(self, parser):
        parser.add_argument('--url', required=True, help='Root H5AI URL to crawl')
        parser.add_argument('--force', action='store_true', help='Force re-fetch metadata even for unchanged directories')
        parser.add_argument('--retry-failed', action='store_true', help='Retry previously failed parses')
        parser.add_argument('--max-items', type=int, default=None, help='Maximum items to scan (default: unlimited)')
        parser.add_argument('--timeout', type=int, default=8, help='Per-request timeout in seconds')
        parser.add_argument('--verbose', action='store_true', help='Verbose output')

    def handle(self, *args, **options):
        root = options['url']
        force = options['force']
        retry_failed = options['retry_failed']
        max_items = options['max_items']
        timeout = options['timeout']
        self.verbose = options.get('verbose', False)
        
        # Statistics
        stats = {
            'total_scanned': 0,
            'new_movies': 0,
            'updated_movies': 0,
            'metadata_fetched': 0,
            'skipped_unchanged': 0,
            'skipped_scheduled': 0,
            'failed_no_media': 0,
            'failed_timeout': 0,
            'failed_error': 0,
            'direct_files': 0,
        }
        
        self.stdout.write(self.style.MIGRATE_HEADING(f"Starting crawl at {root}"))
        limit_str = str(max_items) if max_items else 'unlimited'
        self.stdout.write(f"Options: force={force}, retry_failed={retry_failed}, max_items={limit_str}")
        
        # Optionally retry failed parses first
        if retry_failed:
            self._retry_failed_parses(stats, force, timeout)
        
        # Track for early exit detection
        last_progress_report = 0
        items_since_last_action = 0
        
        # Main crawl
        try:
            for item in crawl_h5ai_recursive(root, per_request_timeout=timeout, max_items=max_items):
                stats['total_scanned'] += 1
                items_since_last_action += 1
                
                # Progress output every 100 items
                if stats['total_scanned'] - last_progress_report >= 100:
                    last_progress_report = stats['total_scanned']
                    self.stdout.write(
                        f"  Progress: {stats['total_scanned']} scanned, "
                        f"{stats['new_movies']} new, {stats['skipped_unchanged']} skipped..."
                    )
                
                if item['is_dir']:
                    # Process as directory containing media files
                    old_new = stats['new_movies']
                    old_updated = stats['updated_movies']
                    self._process_directory(item, stats, force, timeout)
                    if stats['new_movies'] > old_new or stats['updated_movies'] > old_updated:
                        items_since_last_action = 0
                else:
                    # Check if it's a media file directly
                    name = item['name'].lower()
                    if any(name.endswith(ext) for ext in MEDIA_EXTS):
                        old_new = stats['new_movies']
                        self._process_media_file(item, stats, force)
                        if stats['new_movies'] > old_new:
                            items_since_last_action = 0
            
            # Check if nothing was done
            if stats['total_scanned'] > 0 and stats['new_movies'] == 0 and stats['updated_movies'] == 0:
                self.stdout.write(self.style.WARNING(
                    "\n[OK] All items already processed - nothing new to fetch!"
                ))
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Crawler failed: {e}"))
            import traceback
            if self.verbose:
                self.stdout.write(traceback.format_exc())
        
        # Print summary
        self._print_summary(stats)

    def _should_skip_directory(self, dir_url: str, remote_mod, force: bool) -> bool:
        """Check if directory should be skipped based on modification time"""
        if force:
            return False
        
        try:
            cached = CrawledDirectory.objects.get(url=dir_url)
            if remote_mod and cached.remote_modified:
                # Both should be timezone-aware now
                if remote_mod <= cached.remote_modified:
                    return True
        except CrawledDirectory.DoesNotExist:
            pass
        
        return False

    def _update_directory_cache(self, dir_url: str, remote_mod, movie_count: int):
        """Update or create directory cache entry"""
        CrawledDirectory.objects.update_or_create(
            url=dir_url,
            defaults={
                'remote_modified': remote_mod,
                'movie_count': movie_count,
            }
        )

    def _log_failed_parse(self, name: str, url: str, reason: str, raw: str = '', error: str = ''):
        """Log a failed parse attempt"""
        FailedParse.objects.update_or_create(
            url=url,
            defaults={
                'name': name,
                'reason': reason,
                'raw_text': raw[:1000] if raw else '',
                'error_message': error[:500] if error else '',
            }
        )

    def _remove_from_failed(self, url: str):
        """Remove entry from failed parses if it exists"""
        FailedParse.objects.filter(url=url).delete()

    def _process_media_file(self, item: dict, stats: dict, force: bool):
        """Process a direct media file (not inside a folder)"""
        name = item['name']
        href = item['url']
        raw = item.get('raw', '')
        remote_mod = extract_last_modified_from_text(raw)
        
        # Parse title and year from filename
        title, year_str = parse_title_year(name)
        year = None
        if year_str:
            try:
                year = int(year_str)
            except ValueError:
                year = None
        
        # Check for existing movie by file_url (the unique key)
        try:
            movie, is_new = Movie.objects.get_or_create(
                file_url=href,
                defaults={
                    'title': title,
                    'year': year,
                    'year_str': year_str,
                }
            )
        except IntegrityError:
            # Handle race condition
            movie = Movie.objects.filter(file_url=href).first()
            if not movie:
                stats['failed_error'] += 1
                return
            is_new = False
        
        # Check if scheduled for later
        if movie.next_crawl and movie.next_crawl > timezone.now():
            stats['skipped_scheduled'] += 1
            return
        
        # Check if already fetched and unchanged
        if not force and movie.fetched and not is_new:
            stats['skipped_unchanged'] += 1
            return
        
        stats['direct_files'] += 1
        
        # Fetch metadata
        self._fetch_movie_metadata(movie, remote_mod, stats, is_new)
        self.stdout.write(self.style.SUCCESS(f"Processed file: {title}"))

    def _process_directory(self, item: dict, stats: dict, force: bool, timeout: int):
        """Process a single directory item"""
        name = item['name']
        href = item['url']
        raw = item.get('raw', '')
        remote_mod = extract_last_modified_from_text(raw)
        
        # Check if directory should be skipped
        if self._should_skip_directory(href, remote_mod, force):
            stats['skipped_unchanged'] += 1
            if self.verbose:
                self.stdout.write(self.style.NOTICE(f"Skipping unchanged: {name}"))
            return
        
        # Parse title and year from folder name
        title, year_str = parse_title_year(name)
        year = None
        if year_str:
            try:
                year = int(year_str)
            except ValueError:
                year = None
        
        # Find media files inside this directory
        try:
            media = find_media_in_directory(href, timeout=timeout)
        except requests.exceptions.Timeout:
            stats['failed_timeout'] += 1
            self._log_failed_parse(name, href, 'timeout', raw)
            self.stdout.write(self.style.WARNING(f"Timeout finding media in: {name}"))
            return
        except Exception as e:
            stats['failed_error'] += 1
            self._log_failed_parse(name, href, 'network_error', raw, str(e))
            if self.verbose:
                self.stdout.write(self.style.ERROR(f"Error finding media in {name}: {e}"))
            return
        
        if not media:
            stats['failed_no_media'] += 1
            self._log_failed_parse(name, href, 'no_media', raw)
            if self.verbose:
                self.stdout.write(self.style.NOTICE(f"No media in: {name}"))
            return
        
        # Take first media file
        media_name, media_link, media_raw = media[0]
        
        # Check for existing movie by file_url (the unique key)
        try:
            movie, is_new = Movie.objects.get_or_create(
                file_url=media_link,
                defaults={
                    'title': title,
                    'year': year,
                    'year_str': year_str,
                    'directory_url': href,
                }
            )
        except IntegrityError:
            # Handle race condition
            movie = Movie.objects.filter(file_url=media_link).first()
            if not movie:
                stats['failed_error'] += 1
                return
            is_new = False
        
        # Check if scheduled for later
        if movie.next_crawl and movie.next_crawl > timezone.now():
            stats['skipped_scheduled'] += 1
            if self.verbose:
                self.stdout.write(self.style.WARNING(f"Scheduled skip: {movie.title}"))
            return
        
        # Check if remote hasn't changed
        if not force and remote_mod and movie.remote_modified and not is_new:
            if remote_mod <= movie.remote_modified and movie.fetched:
                stats['skipped_unchanged'] += 1
                if self.verbose:
                    self.stdout.write(self.style.NOTICE(f"No change: {movie.title}"))
                return
        
        # Fetch metadata if needed
        if not movie.fetched or force or is_new:
            self._fetch_movie_metadata(movie, remote_mod, stats, is_new)
        else:
            if self.verbose:
                self.stdout.write(self.style.NOTICE(f"Already fetched: {movie.title}"))
        
        # Update directory cache and remove from failed
        self._update_directory_cache(href, remote_mod, 1)
        self._remove_from_failed(href)

    def _fetch_movie_metadata(self, movie: Movie, remote_mod, stats: dict, is_new: bool):
        """Fetch and save movie metadata"""
        try:
            details = fetch_poster_and_details(movie.title, movie.year_str or '')
            
            if details:
                movie.poster_url = details.get('poster_url') or movie.poster_url
                movie.popularity = details.get('popularity') or movie.popularity
                movie.vote_count = details.get('vote_count') or movie.vote_count
                movie.vote_average = details.get('vote_average') or movie.vote_average
                movie.fetched = True
                movie.metadata_status = 'ok'
                movie.last_crawled = timezone.now()
                movie.remote_modified = remote_mod or movie.remote_modified
                movie.next_crawl = None
                movie.save()
                
                stats['metadata_fetched'] += 1
                if is_new:
                    stats['new_movies'] += 1
                else:
                    stats['updated_movies'] += 1
                
                self.stdout.write(self.style.SUCCESS(f"Fetched: {movie.title}"))
            else:
                movie.metadata_status = 'missing'
                movie.next_crawl = timezone.now() + timedelta(hours=1)
                movie.last_crawled = timezone.now()
                movie.remote_modified = remote_mod or movie.remote_modified
                movie.save()
                
                if is_new:
                    stats['new_movies'] += 1
                
                self.stdout.write(self.style.WARNING(f"Missing metadata: {movie.title}"))
                
        except requests.exceptions.Timeout:
            stats['failed_timeout'] += 1
            movie.metadata_status = 'missing'
            movie.next_crawl = timezone.now() + timedelta(hours=1)
            movie.last_crawled = timezone.now()
            movie.save()
            self.stdout.write(self.style.WARNING(f"Timeout fetching metadata: {movie.title}"))
            
        except Exception as e:
            stats['failed_error'] += 1
            movie.metadata_status = 'failed'
            movie.next_crawl = timezone.now() + timedelta(hours=6)
            movie.last_crawled = timezone.now()
            movie.save()
            self.stdout.write(self.style.ERROR(f"Error fetching {movie.title}: {e}"))

    def _retry_failed_parses(self, stats: dict, force: bool, timeout: int):
        """Retry previously failed parse entries"""
        failed_entries = FailedParse.objects.filter(retry_count__gt=0).order_by('retry_count')
        
        if not failed_entries.exists():
            self.stdout.write("No failed entries marked for retry")
            return
        
        self.stdout.write(self.style.MIGRATE_HEADING(f"Retrying {failed_entries.count()} failed entries"))
        
        for entry in failed_entries:
            item = {
                'name': entry.name,
                'url': entry.url,
                'is_dir': True,
                'raw': entry.raw_text,
            }
            self._process_directory(item, stats, force, timeout)
            
            # Decrement retry count if still failed
            if FailedParse.objects.filter(url=entry.url).exists():
                FailedParse.objects.filter(url=entry.url).update(
                    retry_count=max(0, entry.retry_count - 1)
                )

    def _print_summary(self, stats: dict):
        """Print crawl summary"""
        self.stdout.write(self.style.MIGRATE_HEADING("\n=== Crawl Summary ==="))
        self.stdout.write(self.style.SUCCESS(f"Total items scanned: {stats['total_scanned']}"))
        self.stdout.write(self.style.SUCCESS(f"New movies added: {stats['new_movies']}"))
        self.stdout.write(self.style.SUCCESS(f"Movies updated: {stats['updated_movies']}"))
        self.stdout.write(self.style.SUCCESS(f"Metadata fetched: {stats['metadata_fetched']}"))
        self.stdout.write(self.style.SUCCESS(f"Direct files processed: {stats['direct_files']}"))
        self.stdout.write(self.style.WARNING(f"Skipped (unchanged): {stats['skipped_unchanged']}"))
        self.stdout.write(self.style.WARNING(f"Skipped (scheduled): {stats['skipped_scheduled']}"))
        self.stdout.write(self.style.ERROR(f"Failed (no media in folder): {stats['failed_no_media']}"))
        self.stdout.write(self.style.ERROR(f"Failed (timeout): {stats['failed_timeout']}"))
        self.stdout.write(self.style.ERROR(f"Failed (error): {stats['failed_error']}"))
        
        # Show failed parse counts
        failed_count = FailedParse.objects.count()
        if failed_count > 0:
            self.stdout.write(self.style.NOTICE(f"\nTotal failed parses in database: {failed_count}"))
            self.stdout.write("Run with --retry-failed to retry, or use admin to mark entries for retry")
        
        self.stdout.write(self.style.SUCCESS("\nDone!"))
