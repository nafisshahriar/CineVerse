"""Push local SQLite catalog data into an online PostgreSQL (or compatible) database."""

import json
import os
import sqlite3
from pathlib import Path

import dj_database_url
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.core.management.color import no_style
from django.db import connections, transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from movies.models import (
    CrawledDirectory,
    FailedParse,
    MediaSource,
    Movie,
    MovieDetail,
)

TARGET_ALIAS = 'target_online'
BATCH_SIZE = 1000


def _parse_dt(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str):
        return value
    dt = parse_datetime(value)
    if dt is not None and timezone.is_naive(dt) and settings.USE_TZ:
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


def _parse_d(value):
    if value is None or value == '':
        return None
    return parse_date(value) if isinstance(value, str) else value


def _parse_json(value, default):
    if value is None or value == '':
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(value)
        return parsed if parsed is not None else default
    except (TypeError, ValueError):
        return default


def _clamp(value, max_len: int, default: str = ''):
    if value is None:
        return default
    text = str(value)
    return text[:max_len] if len(text) > max_len else text


class Command(BaseCommand):
    help = (
        "Push all local SQLite catalog tables (MediaSource, Movie, MovieDetail, "
        "CrawledDirectory, FailedParse) to an online DATABASE_URL without touching admin users."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--source-sqlite',
            type=str,
            default=str(Path(settings.BASE_DIR) / 'db.sqlite3'),
            help='Path to local SQLite database file (default: <BASE_DIR>/db.sqlite3)',
        )
        parser.add_argument(
            '--target-url',
            type=str,
            default=os.getenv('DATABASE_URL', '').strip().strip('"').strip("'"),
            help='Target DATABASE_URL (defaults to DATABASE_URL env var)',
        )
        parser.add_argument(
            '--replace',
            action='store_true',
            help='Clear target catalog tables before inserting (preserves auth users & migrations)',
        )
        parser.add_argument(
            '--skip-crawldirs',
            action='store_true',
            help='Skip syncing CrawledDirectory cache rows to speed up initial push',
        )

    def _register_target_db(self, target_url: str):
        cleaned = (target_url or '').strip().strip('"').strip("'")
        if not cleaned:
            raise CommandError(
                "No target database URL provided. Set DATABASE_URL in your environment/.env "
                "or pass --target-url 'postgresql://...'"
            )

        db_cfg = dj_database_url.parse(cleaned, conn_max_age=300)
        db_cfg.setdefault('ATOMIC_REQUESTS', False)
        db_cfg.setdefault('AUTOCOMMIT', True)
        db_cfg.setdefault('CONN_HEALTH_CHECKS', False)
        db_cfg.setdefault('OPTIONS', {})
        db_cfg.setdefault('TIME_ZONE', None)

        settings.DATABASES[TARGET_ALIAS] = db_cfg
        connections.databases[TARGET_ALIAS] = db_cfg
        return connections[TARGET_ALIAS]

    def _read_sqlite_rows(self, source_path: Path, skip_crawldirs: bool) -> dict:
        if not source_path.exists():
            raise CommandError(f"Source SQLite file not found: {source_path}")

        conn = sqlite3.connect(str(source_path))
        conn.row_factory = sqlite3.Row
        try:
            existing_tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            if 'movies_movie' not in existing_tables:
                raise CommandError(f"Source file {source_path} does not contain a movies_movie table.")

            def fetch_all(table_name: str):
                if table_name not in existing_tables:
                    return []
                return [dict(r) for r in conn.execute(f'SELECT * FROM "{table_name}"').fetchall()]

            return {
                'media_sources': fetch_all('movies_mediasource'),
                'movies': fetch_all('movies_movie'),
                'movie_details': fetch_all('movies_moviedetail'),
                'crawled_dirs': [] if skip_crawldirs else fetch_all('movies_crawleddirectory'),
                'failed_parses': fetch_all('movies_failedparse'),
            }
        finally:
            conn.close()

    def _build_media_source(self, row: dict, include_id: bool = True) -> MediaSource:
        kwargs = {
            'name': _clamp(row.get('name'), 100),
            'url': _clamp(row.get('url'), 1000),
            'source_type': _clamp(row.get('source_type'), 50, 'h5ai'),
            'is_active': bool(row.get('is_active', True)),
            'created_at': _parse_dt(row.get('created_at')) or timezone.now(),
            'last_crawled': _parse_dt(row.get('last_crawled')),
        }
        if include_id and row.get('id') is not None:
            kwargs['id'] = row['id']
        return MediaSource(**kwargs)

    def _build_movie(self, row: dict, include_id: bool = True) -> Movie:
        pop = row.get('popularity')
        vc = row.get('vote_count')
        va = row.get('vote_average')
        kwargs = {
            'title': _clamp(row.get('title'), 512),
            'clean_title': _clamp(row.get('clean_title'), 512),
            'year': row.get('year'),
            'year_str': _clamp(row.get('year_str'), 10, default=None),
            'poster_url': _clamp(row.get('poster_url'), 512, default=None),
            'file_url': _clamp(row.get('file_url'), 1000),
            'directory_url': _clamp(row.get('directory_url'), 1000, default=None),
            'source': _clamp(row.get('source'), 64),
            'resolution': _clamp(row.get('resolution'), 20),
            'quality': _clamp(row.get('quality'), 32),
            'language': _clamp(row.get('language'), 40),
            'genres': _clamp(row.get('genres'), 255),
            'group_key': _clamp(row.get('group_key'), 512),
            'is_primary_copy': bool(row.get('is_primary_copy', True)),
            'popularity': float(pop) if pop is not None else None,
            'vote_count': int(vc) if vc is not None else None,
            'vote_average': float(va) if va is not None else None,
            'imdb_id': _clamp(row.get('imdb_id'), 20),
            'imdb_rating': row.get('imdb_rating'),
            'runtime': row.get('runtime'),
            'fetched': bool(row.get('fetched', False)),
            'metadata_status': _clamp(row.get('metadata_status'), 32, 'missing'),
            'last_crawled': _parse_dt(row.get('last_crawled')),
            'remote_modified': _parse_dt(row.get('remote_modified')),
            'next_crawl': _parse_dt(row.get('next_crawl')),
            'last_updated': _parse_dt(row.get('last_updated')) or timezone.now(),
        }
        if include_id and row.get('id') is not None:
            kwargs['id'] = row['id']
        return Movie(**kwargs)

    def _build_detail(self, row: dict, movie_id: int) -> MovieDetail:
        return MovieDetail(
            movie_id=movie_id,
            tmdb_id=row.get('tmdb_id'),
            overview=row.get('overview') or '',
            genres=_parse_json(row.get('genres'), []),
            runtime=row.get('runtime'),
            release_date=_parse_d(row.get('release_date')),
            backdrop_url=_clamp(row.get('backdrop_url'), 512, default=None),
            trailer_key=_clamp(row.get('trailer_key'), 64),
            trailer_url=_clamp(row.get('trailer_url'), 512, default=None),
            tagline=_clamp(row.get('tagline'), 512),
            imdb_id=_clamp(row.get('imdb_id'), 20),
            imdb_rating=row.get('imdb_rating'),
            original_language=_clamp(row.get('original_language'), 10),
            budget=row.get('budget'),
            revenue=row.get('revenue'),
            production_companies=_parse_json(row.get('production_companies'), []),
            cast=_parse_json(row.get('cast'), []),
            director=_parse_json(row.get('director'), {}),
            fetched_at=_parse_dt(row.get('fetched_at')) or timezone.now(),
        )

    def _build_crawl_dir(self, row: dict, include_id: bool = True) -> CrawledDirectory:
        kwargs = {
            'url': _clamp(row.get('url'), 1000),
            'remote_modified': _parse_dt(row.get('remote_modified')),
            'last_crawled': _parse_dt(row.get('last_crawled')) or timezone.now(),
            'movie_count': int(row.get('movie_count') or 0),
        }
        if include_id and row.get('id') is not None:
            kwargs['id'] = row['id']
        return CrawledDirectory(**kwargs)

    def _build_failed_parse(self, row: dict, include_id: bool = True) -> FailedParse:
        kwargs = {
            'url': _clamp(row.get('url'), 1000),
            'name': _clamp(row.get('name'), 512),
            'reason': _clamp(row.get('reason'), 32, 'unknown'),
            'raw_text': row.get('raw_text') or '',
            'error_message': row.get('error_message') or '',
            'created_at': _parse_dt(row.get('created_at')) or timezone.now(),
            'updated_at': _parse_dt(row.get('updated_at')) or timezone.now(),
            'retry_count': int(row.get('retry_count') or 0),
        }
        if include_id and row.get('id') is not None:
            kwargs['id'] = row['id']
        return FailedParse(**kwargs)

    def _sync_unique_table(self, model_cls, rows: list, key_field: str, builder_fn):
        """Fast batched create+update by unique key when preserving existing online rows."""
        if not rows:
            return 0, 0
        deduped = {}
        for r in rows:
            max_len = 1000 if key_field != 'name' else 100
            key_val = _clamp(r.get(key_field), max_len)
            if key_val:
                deduped[key_val] = r

        existing_map = dict(
            model_cls.objects.using(TARGET_ALIAS).values_list(key_field, 'id')
        )
        to_create = []
        to_update = []
        update_fields = [
            f.name for f in model_cls._meta.concrete_fields
            if not f.primary_key and f.name != key_field
        ]

        for key_val, row in deduped.items():
            if key_val in existing_map:
                obj = builder_fn(row, include_id=False)
                obj.id = existing_map[key_val]
                to_update.append(obj)
            else:
                to_create.append(builder_fn(row, include_id=False))

        if to_create:
            model_cls.objects.using(TARGET_ALIAS).bulk_create(to_create, batch_size=BATCH_SIZE)
        if to_update:
            model_cls.objects.using(TARGET_ALIAS).bulk_update(
                to_update, fields=update_fields, batch_size=BATCH_SIZE
            )
        return len(to_create), len(to_update)

    def _reset_target_sequences(self, target_conn):
        models = [MediaSource, Movie, MovieDetail, CrawledDirectory, FailedParse]
        sql_list = target_conn.ops.sequence_reset_sql(no_style(), models)
        if not sql_list:
            return
        with target_conn.cursor() as cursor:
            for sql in sql_list:
                cursor.execute(sql)

    def handle(self, *args, **options):
        source_path = Path(options['source_sqlite']).resolve()
        target_url = options['target_url']
        replace_mode = options['replace']
        skip_crawldirs = options['skip_crawldirs']

        self.stdout.write(f"Reading local SQLite catalog from: {source_path}")
        data = self._read_sqlite_rows(source_path, skip_crawldirs=skip_crawldirs)
        self.stdout.write(
            f"Loaded source rows -> Movies: {len(data['movies'])}, "
            f"Details: {len(data['movie_details'])}, Sources: {len(data['media_sources'])}, "
            f"CrawlDirs: {len(data['crawled_dirs'])}, Failed: {len(data['failed_parses'])}"
        )

        target_conn = self._register_target_db(target_url)
        try:
            self.stdout.write(f"Applying migrations on target ({target_conn.vendor})...")
            call_command('migrate', database=TARGET_ALIAS, interactive=False, verbosity=1)

            target_has_movies = Movie.objects.using(TARGET_ALIAS).exists()
            full_replace = replace_mode or not target_has_movies

            with transaction.atomic(using=TARGET_ALIAS):
                if full_replace:
                    mode_label = "replace" if replace_mode else "initial bulk load"
                    self.stdout.write(f"Sync mode: {mode_label} (preserving auth users)")
                    if target_has_movies:
                        MovieDetail.objects.using(TARGET_ALIAS).all().delete()
                        FailedParse.objects.using(TARGET_ALIAS).all().delete()
                        CrawledDirectory.objects.using(TARGET_ALIAS).all().delete()
                        Movie.objects.using(TARGET_ALIAS).all().delete()
                        MediaSource.objects.using(TARGET_ALIAS).all().delete()

                    # Deduplicate by unique keys while preserving source IDs
                    sources = {r['name']: self._build_media_source(r) for r in data['media_sources'] if r.get('name')}
                    if sources:
                        MediaSource.objects.using(TARGET_ALIAS).bulk_create(list(sources.values()), batch_size=BATCH_SIZE)

                    movies_by_url = {}
                    valid_movie_ids = set()
                    for r in data['movies']:
                        url_key = _clamp(r.get('file_url'), 1000)
                        if url_key and url_key not in movies_by_url:
                            m = self._build_movie(r)
                            movies_by_url[url_key] = m
                            if m.id is not None:
                                valid_movie_ids.add(m.id)
                    if movies_by_url:
                        Movie.objects.using(TARGET_ALIAS).bulk_create(list(movies_by_url.values()), batch_size=BATCH_SIZE)

                    details = []
                    seen_detail_mids = set()
                    for r in data['movie_details']:
                        mid = r.get('movie_id')
                        if mid in valid_movie_ids and mid not in seen_detail_mids:
                            seen_detail_mids.add(mid)
                            details.append(self._build_detail(r, mid))
                    if details:
                        MovieDetail.objects.using(TARGET_ALIAS).bulk_create(details, batch_size=BATCH_SIZE)

                    cdirs = {_clamp(r.get('url'), 1000): self._build_crawl_dir(r) for r in data['crawled_dirs'] if r.get('url')}
                    if cdirs:
                        CrawledDirectory.objects.using(TARGET_ALIAS).bulk_create(list(cdirs.values()), batch_size=BATCH_SIZE)

                    fails = {_clamp(r.get('url'), 1000): self._build_failed_parse(r) for r in data['failed_parses'] if r.get('url')}
                    if fails:
                        FailedParse.objects.using(TARGET_ALIAS).bulk_create(list(fails.values()), batch_size=BATCH_SIZE)

                    self._reset_target_sequences(target_conn)
                    self.stdout.write(self.style.SUCCESS(
                        f"Pushed {len(movies_by_url)} movies and {len(details)} rich details to target."
                    ))
                else:
                    self.stdout.write("Sync mode: upsert (target has existing movies; pass --replace to overwrite)")
                    ms_c, ms_u = self._sync_unique_table(MediaSource, data['media_sources'], 'name', self._build_media_source)
                    mv_c, mv_u = self._sync_unique_table(Movie, data['movies'], 'file_url', self._build_movie)
                    cd_c, cd_u = self._sync_unique_table(CrawledDirectory, data['crawled_dirs'], 'url', self._build_crawl_dir)
                    fp_c, fp_u = self._sync_unique_table(FailedParse, data['failed_parses'], 'url', self._build_failed_parse)

                    # Match MovieDetail via Movie.file_url so IDs never collide
                    src_id_to_url = {
                        r['id']: _clamp(r.get('file_url'), 1000)
                        for r in data['movies'] if r.get('id') is not None and r.get('file_url')
                    }
                    target_url_to_id = dict(
                        Movie.objects.using(TARGET_ALIAS).values_list('file_url', 'id')
                    )
                    existing_detail_movie_ids = set(
                        MovieDetail.objects.using(TARGET_ALIAS).values_list('movie_id', flat=True)
                    )
                    existing_detail_pk_by_movie = dict(
                        MovieDetail.objects.using(TARGET_ALIAS).values_list('movie_id', 'id')
                    )

                    d_create, d_update = [], []
                    seen_target_mids = set()
                    detail_fields = [
                        f.name for f in MovieDetail._meta.concrete_fields
                        if not f.primary_key and f.name != 'movie'
                    ]
                    for r in data['movie_details']:
                        src_url = src_id_to_url.get(r.get('movie_id'))
                        target_mid = target_url_to_id.get(src_url) if src_url else None
                        if not target_mid or target_mid in seen_target_mids:
                            continue
                        seen_target_mids.add(target_mid)
                        obj = self._build_detail(r, target_mid)
                        if target_mid in existing_detail_movie_ids:
                            obj.id = existing_detail_pk_by_movie[target_mid]
                            d_update.append(obj)
                        else:
                            d_create.append(obj)

                    if d_create:
                        MovieDetail.objects.using(TARGET_ALIAS).bulk_create(d_create, batch_size=BATCH_SIZE)
                    if d_update:
                        MovieDetail.objects.using(TARGET_ALIAS).bulk_update(
                            d_update, fields=detail_fields, batch_size=BATCH_SIZE
                        )

                    self._reset_target_sequences(target_conn)
                    self.stdout.write(self.style.SUCCESS(
                        f"Synced Movies (+{mv_c}/~{mv_u}), Details (+{len(d_create)}/~{len(d_update)}), "
                        f"Sources (+{ms_c}/~{ms_u}), Dirs (+{cd_c}/~{cd_u}), Failed (+{fp_c}/~{fp_u})."
                    ))

            call_command('ensure_admin', database=TARGET_ALIAS)
        finally:
            try:
                connections[TARGET_ALIAS].close()
            except Exception:
                pass
            connections.databases.pop(TARGET_ALIAS, None)
            settings.DATABASES.pop(TARGET_ALIAS, None)
