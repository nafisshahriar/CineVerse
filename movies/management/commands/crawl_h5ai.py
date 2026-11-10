from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError
from django.utils import timezone
from movies.models import Movie, CrawledDirectory, FailedParse
from movies.task_runner import is_task_stopped
from movies.utils import (
    crawl_h5ai_recursive, parse_movie_info, extract_last_modified_from_text,
    find_media_in_directory, MEDIA_EXTS, IGNORED_DIRS, detect_language, detect_source
)
from movies.services import fetch_poster_and_details
from datetime import timedelta
import requests
import os
from urllib.parse import unquote


class Command(BaseCommand):
    help = 'Crawl an H5AI root URL and import movies (timestamp-aware, multi-source crawler)'

    def add_arguments(self, parser):
        parser.add_argument('--url', required=True, help='Root H5AI URL to crawl')
        parser.add_argument('--source', default='', help='Custom source server name or label for imported movies')
        parser.add_argument('--force', action='store_true', help='Force re-fetch metadata even for unchanged directories')
        parser.add_argument('--retry-failed', action='store_true', help='Retry previously failed parses')
        parser.add_argument('--max-items', type=int, default=None, help='Maximum items to scan (default: unlimited)')
        parser.add_argument('--timeout', type=int, default=8, help='Per-request timeout in seconds')
        parser.add_argument('--verbose', action='store_true', help='Verbose output')

    def handle(self, *args, **options):
        root = options['url']
        custom_source = (options.get('source') or '').strip()
        force = options['force']
        retry_failed = options['retry_failed']
        max_items = options['max_items']
        timeout = options['timeout']
        self.verbose = options.get('verbose', False)

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
        src_str = custom_source or 'Auto-detect'
        self.stdout.write(f"Options: source={src_str}, force={force}, retry_failed={retry_failed}, max_items={limit_str}")

        if retry_failed:
            self._retry_failed_parses(stats, force, timeout, custom_source)

        def skip_check(href, raw):
            remote_mod = extract_last_modified_from_text(raw)
            return self._should_skip_directory(href, remote_mod, force)

        self.stdout.write(f"Connecting to media server: {root}...")

        try:
            for item in crawl_h5ai_recursive(root, per_request_timeout=timeout, max_items=max_items, should_skip_dir=skip_check):
                if is_task_stopped():
                    self.stdout.write(self.style.WARNING("\n[STOPPED] Crawl halted by user."))
                    break

                if item.get('error'):
                    self.stdout.write(self.style.ERROR(f"\n[ERROR] {item['message']}"))
                    stats['failed_error'] += 1
                    raise CommandError(item['message'])

                if item.get('warning'):
                    self.stdout.write(self.style.WARNING(f"\n[WARNING] {item['message']}"))
                    continue

                stats['total_scanned'] += 1
                item_name = item.get('name', '').strip()

                # Report real-time progress for background task manager
                if max_items:
                    pct = min(100.0, (stats['total_scanned'] / max_items) * 100)
                    self.stdout.write(
                        f"[{stats['total_scanned']}/{max_items}] ({pct:5.1f}%) | "
                        f"Scanned: {stats['total_scanned']} | New: {stats['new_movies']} | "
                        f"Skipped: {stats['skipped_unchanged']} | Checking: {item_name}"
                    )
                else:
                    self.stdout.write(
                        f"[CRAWL {stats['total_scanned']}] Scanned: {stats['total_scanned']} | "
                        f"New: {stats['new_movies']} | Skipped: {stats['skipped_unchanged']} | "
                        f"Checking: {item_name}"
                    )

                clean_name = item_name.strip(' /\\').lower()
                if clean_name in IGNORED_DIRS:
                    continue

                if item.get('is_skipped'):
                    stats['skipped_unchanged'] += 1
                    self.stdout.write(self.style.NOTICE(f"  [SKIPPED] {item_name} (already scanned & unchanged)"))
                    continue

                if item['is_dir']:
                    raw = item.get('raw', '')
                    remote_mod = extract_last_modified_from_text(raw)
                    self._update_directory_cache(item['url'], remote_mod, 0)
                else:
                    name = item_name.lower()
                    if any(name.endswith(ext) for ext in MEDIA_EXTS):
                        self._process_media_file(item, stats, force, custom_source)

            if custom_source:
                try:
                    from movies.models import MediaSource
                    MediaSource.objects.update_or_create(
                        name=custom_source,
                        defaults={'url': root, 'last_crawled': timezone.now(), 'is_active': True}
                    )
                except Exception:
                    pass

            if stats['total_scanned'] > 0 and stats['new_movies'] == 0 and stats['updated_movies'] == 0 and stats['failed_error'] == 0:
                self.stdout.write(self.style.SUCCESS(
                    f"\n[OK] All {stats['total_scanned']} items checked - catalog is completely up to date!"
                ))

        except CommandError:
            raise
        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Crawler failed: {e}"))
            import traceback
            if self.verbose:
                self.stdout.write(traceback.format_exc())
            raise CommandError(str(e))

        self._print_summary(stats)

    def _should_skip_directory(self, dir_url: str, remote_mod, force: bool) -> bool:
        if force:
            return False
        try:
            cached = CrawledDirectory.objects.get(url=dir_url)
            if remote_mod and cached.remote_modified:
                if remote_mod <= cached.remote_modified:
                    return True
            # If server does not report timestamps, but directory was already crawled
            # within last 24 hours and has known movies, skip re-scanning unless --force is set
            if not remote_mod and cached.movie_count > 0 and cached.last_crawled:
                if (timezone.now() - cached.last_crawled) < timedelta(hours=24):
                    return True
        except CrawledDirectory.DoesNotExist:
            pass
        return False

    def _update_directory_cache(self, dir_url: str, remote_mod, movie_count: int):
        CrawledDirectory.objects.update_or_create(
            url=dir_url,
            defaults={
                'remote_modified': remote_mod,
                'movie_count': movie_count,
            }
        )

    def _log_failed_parse(self, name: str, url: str, reason: str, raw: str = '', error: str = ''):
        clean = name.strip(' /\\').lower()
        if clean in IGNORED_DIRS:
            return
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
        FailedParse.objects.filter(url=url).delete()

    def _process_media_file(self, item: dict, stats: dict, force: bool, custom_source: str = ''):
        name = item['name']
        href = item['url']
        raw = item.get('raw', '')
        remote_mod = extract_last_modified_from_text(raw)
        dir_name = item.get('dir_name', '')

        info = parse_movie_info(dir_name=dir_name, file_name=name, file_url=href)
        source_name = custom_source or info['source'] or detect_source(href)

        try:
            movie, is_new = Movie.objects.get_or_create(
                file_url=href,
                defaults={
                    'title': info['title'],
                    'clean_title': info['clean_title'],
                    'year': info['year'],
                    'year_str': info['year_str'],
                    'source': source_name,
                    'resolution': info['resolution'],
                    'quality': info['quality'],
                    'language': info['language'],
                    'group_key': info['group_key'],
                    'directory_url': item.get('directory_url') or '',
                }
            )
        except IntegrityError:
            movie = Movie.objects.filter(file_url=href).first()
            if not movie:
                stats['failed_error'] += 1
                return
            is_new = False

        if not is_new:
            changed_fields = []
            if not movie.clean_title:
                movie.clean_title = info['clean_title']
                movie.resolution = info['resolution']
                movie.quality = info['quality']
                movie.language = info['language']
                movie.group_key = info['group_key']
                changed_fields.extend(['clean_title', 'resolution', 'quality', 'language', 'group_key'])
            if custom_source and movie.source != custom_source:
                movie.source = custom_source
                changed_fields.append('source')
            if changed_fields:
                movie.save()

        if movie.next_crawl and movie.next_crawl > timezone.now():
            stats['skipped_scheduled'] += 1
            return

        if not force and movie.fetched and not is_new:
            stats['skipped_unchanged'] += 1
            if item.get('directory_url'):
                self._update_directory_cache(item['directory_url'], remote_mod, 1)
            self.stdout.write(self.style.NOTICE(f"  [SKIPPED] {movie.display_title} (unchanged)"))
            return

        stats['direct_files'] += 1
        self._fetch_movie_metadata(movie, remote_mod, stats, is_new)
        if item.get('directory_url'):
            self._update_directory_cache(item['directory_url'], remote_mod, 1)
        self.stdout.write(self.style.SUCCESS(f"Processed file: {movie.display_title} [{movie.source}]"))

    def _process_directory(self, item: dict, stats: dict, force: bool, timeout: int, custom_source: str = ''):
        name = item['name']
        href = item['url']
        raw = item.get('raw', '')
        remote_mod = extract_last_modified_from_text(raw)

        if self._should_skip_directory(href, remote_mod, force):
            stats['skipped_unchanged'] += 1
            if self.verbose:
                self.stdout.write(self.style.NOTICE(f"Skipping unchanged: {name}"))
            return

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
            return

        media_name, media_link, media_raw = media[0]
        info = parse_movie_info(dir_name=name, file_name=media_name, file_url=media_link)
        source_name = custom_source or info['source'] or detect_source(media_link)

        try:
            movie, is_new = Movie.objects.get_or_create(
                file_url=media_link,
                defaults={
                    'title': info['title'],
                    'clean_title': info['clean_title'],
                    'year': info['year'],
                    'year_str': info['year_str'],
                    'source': source_name,
                    'resolution': info['resolution'],
                    'quality': info['quality'],
                    'language': info['language'],
                    'group_key': info['group_key'],
                    'directory_url': href,
                }
            )
        except IntegrityError:
            movie = Movie.objects.filter(file_url=media_link).first()
            if not movie:
                stats['failed_error'] += 1
                return
            is_new = False

        if not is_new:
            changed_fields = []
            if not movie.clean_title:
                movie.clean_title = info['clean_title']
                movie.resolution = info['resolution']
                movie.quality = info['quality']
                movie.language = info['language']
                movie.group_key = info['group_key']
                changed_fields.extend(['clean_title', 'resolution', 'quality', 'language', 'group_key'])
            if custom_source and movie.source != custom_source:
                movie.source = custom_source
                changed_fields.append('source')
            if changed_fields:
                movie.save()

        if movie.next_crawl and movie.next_crawl > timezone.now():
            stats['skipped_scheduled'] += 1
            return

        if not force and remote_mod and movie.remote_modified and not is_new:
            if remote_mod <= movie.remote_modified and movie.fetched:
                stats['skipped_unchanged'] += 1
                return

        if not movie.fetched or force or is_new:
            self._fetch_movie_metadata(movie, remote_mod, stats, is_new)

        self._update_directory_cache(href, remote_mod, len(media))
        self._remove_from_failed(href)

    def _fetch_movie_metadata(self, movie: Movie, remote_mod, stats: dict, is_new: bool):
        search_title = movie.clean_title or movie.title
        try:
            details = fetch_poster_and_details(search_title, movie.year_str or '')

            if details:
                movie.poster_url = details.get('poster_url') or movie.poster_url
                movie.popularity = details.get('popularity') or movie.popularity
                movie.vote_count = details.get('vote_count') or movie.vote_count
                movie.vote_average = details.get('vote_average') or movie.vote_average
                if details.get('genres_str'):
                    movie.genres = details['genres_str']
                elif details.get('genres'):
                    movie.genres = ', '.join(details['genres'])
                movie.fetched = True
                movie.metadata_status = 'ok'
                movie.last_crawled = timezone.now()
                movie.remote_modified = remote_mod or movie.remote_modified
                movie.next_crawl = None
                if details.get('original_language'):
                    movie.language = detect_language(tmdb_code=details['original_language'])
                movie.save()

                stats['metadata_fetched'] += 1
                if is_new:
                    stats['new_movies'] += 1
                else:
                    stats['updated_movies'] += 1

                self.stdout.write(self.style.SUCCESS(f"Fetched: {movie.display_title}"))
            else:
                movie.metadata_status = 'missing'
                movie.next_crawl = timezone.now() + timedelta(hours=6)
                movie.last_crawled = timezone.now()
                movie.remote_modified = remote_mod or movie.remote_modified
                movie.save()

                if is_new:
                    stats['new_movies'] += 1

                self.stdout.write(self.style.WARNING(f"Missing metadata: {movie.display_title}"))

        except requests.exceptions.Timeout:
            stats['failed_timeout'] += 1
            movie.metadata_status = 'missing'
            movie.next_crawl = timezone.now() + timedelta(hours=2)
            movie.last_crawled = timezone.now()
            movie.save()
            self.stdout.write(self.style.WARNING(f"Timeout fetching metadata: {movie.display_title}"))

        except Exception as e:
            stats['failed_error'] += 1
            movie.metadata_status = 'failed'
            movie.next_crawl = timezone.now() + timedelta(hours=12)
            movie.last_crawled = timezone.now()
            movie.save()
            self.stdout.write(self.style.ERROR(f"Error fetching {movie.display_title}: {e}"))

    def _retry_failed_parses(self, stats: dict, force: bool, timeout: int, custom_source: str = ''):
        failed_entries = FailedParse.objects.filter(retry_count__gt=0).order_by('retry_count')
        if not failed_entries.exists():
            return
        self.stdout.write(self.style.MIGRATE_HEADING(f"Retrying {failed_entries.count()} failed entries"))
        for entry in failed_entries:
            item = {
                'name': entry.name,
                'url': entry.url,
                'is_dir': True,
                'raw': entry.raw_text,
            }
            self._process_directory(item, stats, force, timeout, custom_source)
            if FailedParse.objects.filter(url=entry.url).exists():
                FailedParse.objects.filter(url=entry.url).update(
                    retry_count=max(0, entry.retry_count - 1)
                )

    def _print_summary(self, stats: dict):
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
        self.stdout.write(self.style.SUCCESS("\n[OK] Done!"))
