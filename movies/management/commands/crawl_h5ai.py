from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, close_old_connections
from django.utils import timezone
from movies.cache_utils import invalidate_catalog_cache
from movies.models import Movie, CrawledDirectory, FailedParse
from movies.task_runner import is_task_stopped
from movies.utils import (
    crawl_h5ai_recursive, parse_movie_info, extract_last_modified_from_text,
    find_media_in_directory, MEDIA_EXTS, IGNORED_DIRS, detect_language, detect_source
)
from movies.services import fetch_poster_and_details, reconcile_copy_groups
from datetime import timedelta
import requests
import os
from urllib.parse import unquote


class Command(BaseCommand):
    help = 'Crawl an H5AI root URL and import movies (timestamp-aware, multi-source crawler)'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.verbose = False
        self._dir_cache = None
        self._pending_dir_updates = {}
        self._failed_urls = None
        self._movie_cache = None
        self._touched_group_keys = set()

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

        self._load_caches()

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
                    close_old_connections()
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
        finally:
            self._flush_dir_cache_updates()
            if self._touched_group_keys:
                reconcile_res = reconcile_copy_groups(group_keys=self._touched_group_keys)
                if reconcile_res.get('updated'):
                    self.stdout.write(self.style.NOTICE(
                        f"Reconciled {reconcile_res['updated']} primary/copy record(s) across {reconcile_res['groups']} group(s)."
                    ))
            if stats['new_movies'] or stats['updated_movies'] or stats['metadata_fetched'] or custom_source:
                invalidate_catalog_cache()

        self._print_summary(stats)

    def _load_caches(self):
        """Preload catalog skip tables once so remote Postgres crawls avoid per-item round-trips."""
        close_old_connections()
        self._pending_dir_updates = {}
        self._touched_group_keys = set()
        self._dir_cache = {
            url: (remote_modified, movie_count, last_crawled)
            for url, remote_modified, movie_count, last_crawled
            in CrawledDirectory.objects.values_list('url', 'remote_modified', 'movie_count', 'last_crawled')
        }
        self._failed_urls = set(FailedParse.objects.values_list('url', flat=True))
        self._movie_cache = {
            file_url: {
                'id': m_id,
                'clean_title': clean_title,
                'source': source,
                'fetched': fetched,
                'next_crawl': next_crawl,
                'remote_modified': remote_modified,
            }
            for file_url, m_id, clean_title, source, fetched, next_crawl, remote_modified
            in Movie.objects.values_list(
                'file_url', 'id', 'clean_title', 'source', 'fetched', 'next_crawl', 'remote_modified'
            )
        }

    def _flush_dir_cache_updates(self):
        """Persist buffered directory timestamp/count updates in batches."""
        if not self._pending_dir_updates:
            return
        pending = self._pending_dir_updates
        self._pending_dir_updates = {}

        close_old_connections()
        now = timezone.now()
        existing = {
            d.url: d
            for d in CrawledDirectory.objects.filter(url__in=list(pending.keys()))
        }
        to_create = []
        to_update = []
        for url, (remote_mod, movie_count) in pending.items():
            row = existing.get(url)
            if row is None:
                to_create.append(
                    CrawledDirectory(
                        url=url,
                        remote_modified=remote_mod,
                        movie_count=movie_count,
                    )
                )
            else:
                row.remote_modified = remote_mod
                row.movie_count = movie_count
                row.last_crawled = now
                to_update.append(row)

        if to_create:
            CrawledDirectory.objects.bulk_create(to_create, batch_size=200, ignore_conflicts=True)
        if to_update:
            CrawledDirectory.objects.bulk_update(
                to_update,
                ['remote_modified', 'movie_count', 'last_crawled'],
                batch_size=200,
            )

    def _should_skip_directory(self, dir_url: str, remote_mod, force: bool) -> bool:
        if force:
            return False
        safe_url = (dir_url or '')[:1000]
        if self._dir_cache is not None:
            cached_tuple = self._dir_cache.get(safe_url)
            if cached_tuple is None:
                return False
            cached_mod, movie_count, last_crawled = cached_tuple
        else:
            try:
                cached = CrawledDirectory.objects.get(url=safe_url)
                cached_mod = cached.remote_modified
                movie_count = cached.movie_count
                last_crawled = cached.last_crawled
            except CrawledDirectory.DoesNotExist:
                return False

        if remote_mod and cached_mod and remote_mod <= cached_mod:
            return True
        # If server does not report timestamps, but directory was already crawled
        # within last 24 hours and has known movies, skip re-scanning unless --force is set
        if not remote_mod and movie_count > 0 and last_crawled:
            if (timezone.now() - last_crawled) < timedelta(hours=24):
                return True
        return False

    def _update_directory_cache(self, dir_url: str, remote_mod, movie_count: int):
        safe_url = (dir_url or '')[:1000]
        if self._dir_cache is not None:
            now = timezone.now()
            existing = self._dir_cache.get(safe_url)
            if (
                existing is not None
                and existing[0] == remote_mod
                and existing[1] == movie_count
                and existing[2] is not None
                and (now - existing[2]) < timedelta(hours=6)
            ):
                return
            self._dir_cache[safe_url] = (remote_mod, movie_count, now)
            self._pending_dir_updates[safe_url] = (remote_mod, movie_count)
            if len(self._pending_dir_updates) >= 50:
                self._flush_dir_cache_updates()
            return

        CrawledDirectory.objects.update_or_create(
            url=safe_url,
            defaults={
                'remote_modified': remote_mod,
                'movie_count': movie_count,
            }
        )

    def _log_failed_parse(self, name: str, url: str, reason: str, raw: str = '', error: str = ''):
        clean = name.strip(' /\\').lower()
        if clean in IGNORED_DIRS:
            return
        safe_url = (url or '')[:1000]
        close_old_connections()
        FailedParse.objects.update_or_create(
            url=safe_url,
            defaults={
                'name': (name or '')[:512],
                'reason': (reason or '')[:32],
                'raw_text': raw[:1000] if raw else '',
                'error_message': error[:500] if error else '',
            }
        )
        if self._failed_urls is not None:
            self._failed_urls.add(safe_url)

    def _remove_from_failed(self, url: str):
        safe_url = (url or '')[:1000]
        if self._failed_urls is not None:
            if safe_url not in self._failed_urls:
                return
            close_old_connections()
            FailedParse.objects.filter(url=safe_url).delete()
            self._failed_urls.discard(safe_url)
            return
        FailedParse.objects.filter(url=safe_url).delete()

    def _remember_movie(self, movie: Movie):
        if self._movie_cache is None or not movie:
            return
        self._movie_cache[(movie.file_url or '')[:1000]] = {
            'id': movie.id,
            'clean_title': movie.clean_title,
            'source': movie.source,
            'fetched': movie.fetched,
            'next_crawl': movie.next_crawl,
            'remote_modified': movie.remote_modified,
        }

    def _try_skip_cached_movie(
        self, file_url: str, custom_source: str, remote_mod, check_remote_mod: bool, stats: dict
    ):
        """Fast in-memory check for already-known movies to avoid per-file DB queries."""
        if self._movie_cache is None:
            return None
        cached = self._movie_cache.get((file_url or '')[:1000])
        if cached is None or not cached.get('clean_title'):
            return None
        safe_custom_source = (custom_source or '')[:64]
        if safe_custom_source and cached.get('source') != safe_custom_source:
            return None

        now = timezone.now()
        next_crawl = cached.get('next_crawl')
        if next_crawl and next_crawl > now:
            stats['skipped_scheduled'] += 1
            return cached['clean_title']

        if not cached.get('fetched'):
            return None

        if check_remote_mod:
            cached_mod = cached.get('remote_modified')
            if remote_mod and cached_mod and remote_mod <= cached_mod:
                stats['skipped_unchanged'] += 1
                return cached['clean_title']
            return None

        stats['skipped_unchanged'] += 1
        return cached['clean_title']

    def _get_or_create_movie(
        self, file_url: str, directory_url: str, info: dict,
        source_name: str, custom_source: str, stats: dict
    ):
        """Create or fetch a Movie row with field lengths clamped for PostgreSQL compatibility."""
        safe_url = (file_url or '')[:1000]
        safe_dir = (directory_url or '')[:1000]
        safe_title = (info.get('title') or '')[:512]
        safe_clean_title = (info.get('clean_title') or '')[:512]
        safe_source = (source_name or '')[:64]
        safe_resolution = (info.get('resolution') or '')[:20]
        safe_quality = (info.get('quality') or '')[:32]
        safe_language = (info.get('language') or '')[:40]
        safe_group_key = (info.get('group_key') or '')[:512]
        safe_year_str = (info.get('year_str') or '')[:10]
        safe_custom_source = (custom_source or '')[:64]

        close_old_connections()
        try:
            movie, is_new = Movie.objects.get_or_create(
                file_url=safe_url,
                defaults={
                    'title': safe_title,
                    'clean_title': safe_clean_title,
                    'year': info.get('year'),
                    'year_str': safe_year_str,
                    'source': safe_source,
                    'resolution': safe_resolution,
                    'quality': safe_quality,
                    'language': safe_language,
                    'group_key': safe_group_key,
                    'directory_url': safe_dir,
                }
            )
        except IntegrityError:
            movie = Movie.objects.filter(file_url=safe_url).first()
            if not movie:
                stats['failed_error'] += 1
                return None, False
            is_new = False

        if not is_new:
            changed_fields = []
            if not movie.clean_title:
                movie.clean_title = safe_clean_title
                movie.resolution = safe_resolution
                movie.quality = safe_quality
                movie.language = safe_language
                movie.group_key = safe_group_key
                changed_fields.extend(['clean_title', 'resolution', 'quality', 'language', 'group_key'])
            if safe_custom_source and movie.source != safe_custom_source:
                movie.source = safe_custom_source
                changed_fields.append('source')
            if changed_fields:
                movie.save()

        if movie.group_key and (is_new or 'group_key' in (changed_fields if not is_new else [])):
            self._touched_group_keys.add(movie.group_key)

        self._remember_movie(movie)
        return movie, is_new

    def _process_media_file(self, item: dict, stats: dict, force: bool, custom_source: str = ''):
        name = item['name']
        href = item['url']
        raw = item.get('raw', '')
        remote_mod = extract_last_modified_from_text(raw)
        dir_name = item.get('dir_name', '')

        if not force:
            skipped_title = self._try_skip_cached_movie(
                href, custom_source, remote_mod, check_remote_mod=False, stats=stats
            )
            if skipped_title is not None:
                if item.get('directory_url'):
                    self._update_directory_cache(item['directory_url'], remote_mod, 1)
                self.stdout.write(self.style.NOTICE(f"  [SKIPPED] {skipped_title} (unchanged)"))
                return

        info = parse_movie_info(dir_name=dir_name, file_name=name, file_url=href)
        source_name = custom_source or info['source'] or detect_source(href)

        movie, is_new = self._get_or_create_movie(
            file_url=href,
            directory_url=item.get('directory_url') or '',
            info=info,
            source_name=source_name,
            custom_source=custom_source,
            stats=stats,
        )
        if not movie:
            return

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
        if not force:
            skipped_title = self._try_skip_cached_movie(
                media_link, custom_source, remote_mod, check_remote_mod=True, stats=stats
            )
            if skipped_title is not None:
                return

        info = parse_movie_info(dir_name=name, file_name=media_name, file_url=media_link)
        source_name = custom_source or info['source'] or detect_source(media_link)

        movie, is_new = self._get_or_create_movie(
            file_url=media_link,
            directory_url=href,
            info=info,
            source_name=source_name,
            custom_source=custom_source,
            stats=stats,
        )
        if not movie:
            return

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
            close_old_connections()

            if details:
                movie.poster_url = (details.get('poster_url') or movie.poster_url or '')[:512]
                movie.popularity = details.get('popularity') or movie.popularity
                movie.vote_count = details.get('vote_count') or movie.vote_count
                movie.vote_average = details.get('vote_average') or movie.vote_average
                if details.get('genres_str'):
                    movie.genres = details['genres_str'][:255]
                elif details.get('genres'):
                    movie.genres = ', '.join(details['genres'])[:255]
                movie.fetched = True
                movie.metadata_status = 'ok'
                movie.last_crawled = timezone.now()
                movie.remote_modified = remote_mod or movie.remote_modified
                movie.next_crawl = None
                if details.get('original_language'):
                    movie.language = (detect_language(tmdb_code=details['original_language']) or '')[:40]
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
            close_old_connections()
            stats['failed_timeout'] += 1
            movie.metadata_status = 'missing'
            movie.next_crawl = timezone.now() + timedelta(hours=2)
            movie.last_crawled = timezone.now()
            movie.save()
            self.stdout.write(self.style.WARNING(f"Timeout fetching metadata: {movie.display_title}"))

        except Exception as e:
            close_old_connections()
            stats['failed_error'] += 1
            movie.metadata_status = 'failed'
            movie.next_crawl = timezone.now() + timedelta(hours=12)
            movie.last_crawled = timezone.now()
            movie.save()
            self.stdout.write(self.style.ERROR(f"Error fetching {movie.display_title}: {e}"))
        finally:
            if movie.group_key:
                self._touched_group_keys.add(movie.group_key)
            self._remember_movie(movie)

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
