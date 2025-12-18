from django.contrib import admin
from django.db.models import F
from .models import Movie, CrawledDirectory, FailedParse, MovieDetail


@admin.register(Movie)
class MovieAdmin(admin.ModelAdmin):
    list_display = ('title', 'year', 'metadata_status', 'fetched', 'vote_average', 'popularity')
    search_fields = ('title', 'year_str')
    list_filter = ('fetched', 'metadata_status', 'year')
    ordering = ('title',)
    readonly_fields = ('last_crawled', 'last_updated')


@admin.register(CrawledDirectory)
class CrawledDirectoryAdmin(admin.ModelAdmin):
    list_display = ('url', 'remote_modified', 'last_crawled', 'movie_count')
    search_fields = ('url',)
    readonly_fields = ('last_crawled',)
    ordering = ('-last_crawled',)


@admin.register(FailedParse)
class FailedParseAdmin(admin.ModelAdmin):
    list_display = ('name', 'reason', 'retry_count', 'created_at', 'updated_at')
    list_filter = ('reason', 'retry_count')
    search_fields = ('name', 'url')
    readonly_fields = ('created_at', 'updated_at')
    ordering = ('-created_at',)
    actions = ['mark_for_retry', 'clear_retry_count']
    
    @admin.action(description="Mark selected for retry (increment retry count)")
    def mark_for_retry(self, request, queryset):
        updated = queryset.update(retry_count=F('retry_count') + 1)
        self.message_user(request, f"Marked {updated} entries for retry")
    
    @admin.action(description="Reset retry count to 0")
    def clear_retry_count(self, request, queryset):
        updated = queryset.update(retry_count=0)
        self.message_user(request, f"Reset retry count for {updated} entries")


@admin.register(MovieDetail)
class MovieDetailAdmin(admin.ModelAdmin):
    list_display = ('movie', 'tmdb_id', 'runtime', 'release_date', 'fetched_at')
    search_fields = ('movie__title', 'tmdb_id')
    readonly_fields = ('fetched_at',)
    list_filter = ('original_language',)

