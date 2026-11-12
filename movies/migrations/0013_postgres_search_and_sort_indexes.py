"""Create PostgreSQL-only partial sort and trigram search indexes (no-op on SQLite)."""

from django.db import migrations, transaction


def create_postgres_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return

    with schema_editor.connection.cursor() as cursor:
        # 1. Partial composite B-tree indexes matching primary-copy browse sorts
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS movies_movie_primary_newest_idx
            ON movies_movie (year DESC NULLS LAST, last_crawled DESC NULLS LAST, id DESC)
            WHERE is_primary_copy = true;
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS movies_movie_primary_popularity_idx
            ON movies_movie (popularity DESC NULLS LAST, id DESC)
            WHERE is_primary_copy = true;
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS movies_movie_primary_rating_idx
            ON movies_movie (imdb_rating DESC NULLS LAST, vote_count DESC NULLS LAST, vote_average DESC NULLS LAST, id DESC)
            WHERE is_primary_copy = true;
            """
        )

        # 2. Enable pg_trgm (preferring Supabase's extensions schema when present)
        cursor.execute("SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'pg_trgm'")
        row = cursor.fetchone()
        trgm_schema = row[0] if row else None

        if not trgm_schema:
            cursor.execute("SELECT 1 FROM pg_namespace WHERE nspname = 'extensions'")
            has_extensions_schema = cursor.fetchone() is not None
            target_schema = 'extensions' if has_extensions_schema else 'public'
            try:
                with transaction.atomic(using=schema_editor.connection.alias):
                    cursor.execute(f"CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA {target_schema}")
                trgm_schema = target_schema
            except Exception:
                # Non-superuser Postgres environments may disallow CREATE EXTENSION
                trgm_schema = None

        if trgm_schema:
            opclass = f"{trgm_schema}.gin_trgm_ops"
            cursor.execute(
                f"""
                CREATE INDEX IF NOT EXISTS movies_movie_clean_title_upper_trgm_idx
                ON movies_movie USING gin (UPPER(clean_title::text) {opclass});
                """
            )
            cursor.execute(
                f"""
                CREATE INDEX IF NOT EXISTS movies_movie_title_upper_trgm_idx
                ON movies_movie USING gin (UPPER(title::text) {opclass});
                """
            )


def drop_postgres_indexes(apps, schema_editor):
    if schema_editor.connection.vendor != 'postgresql':
        return

    with schema_editor.connection.cursor() as cursor:
        cursor.execute("DROP INDEX IF EXISTS movies_movie_clean_title_upper_trgm_idx;")
        cursor.execute("DROP INDEX IF EXISTS movies_movie_title_upper_trgm_idx;")
        cursor.execute("DROP INDEX IF EXISTS movies_movie_primary_rating_idx;")
        cursor.execute("DROP INDEX IF EXISTS movies_movie_primary_popularity_idx;")
        cursor.execute("DROP INDEX IF EXISTS movies_movie_primary_newest_idx;")


class Migration(migrations.Migration):

    dependencies = [
        ('movies', '0012_remove_movie_movies_movi_group_k_f2bcb8_idx'),
    ]

    operations = [
        migrations.RunPython(create_postgres_indexes, reverse_code=drop_postgres_indexes),
    ]
