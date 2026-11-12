"""Cache keys and invalidation helpers for catalog summary metrics."""

from django.core.cache import cache

INDEX_SUMMARY_CACHE_KEY = 'cineverse:index_summary:v1'


def invalidate_catalog_cache() -> None:
    """Clear cached catalog summary counts and source lists after catalog mutations."""
    cache.delete(INDEX_SUMMARY_CACHE_KEY)
