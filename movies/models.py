from django.db import models
from django.utils import timezone


class Movie(models.Model):
    title = models.CharField(max_length=512, db_index=True)
    clean_title = models.CharField(max_length=512, blank=True, default='', db_index=True)
    year = models.IntegerField(null=True, blank=True, db_index=True)
    year_str = models.CharField(max_length=10, blank=True, null=True)  # Original string
    poster_url = models.URLField(blank=True, null=True)
    file_url = models.URLField(unique=True)  # Unique constraint on file_url alone
    directory_url = models.URLField(blank=True, null=True)  # Parent directory
    source = models.CharField(max_length=64, blank=True, default='', db_index=True)  # e.g. "Dhaka-Flix 14", "E-Box"
    resolution = models.CharField(max_length=20, blank=True, default='', db_index=True)  # e.g. "4K", "1080p", "720p"
    quality = models.CharField(max_length=32, blank=True, default='')  # e.g. "BluRay", "WEB-DL"
    language = models.CharField(max_length=40, blank=True, default='', db_index=True)  # e.g. "English", "Korean"
    genres = models.CharField(max_length=255, blank=True, default='', db_index=True)  # e.g. "Action, Drama, Thriller"
    group_key = models.CharField(max_length=512, blank=True, default='', db_index=True)  # key to group duplicate copies
    is_primary_copy = models.BooleanField(default=True, db_index=True)  # True for the best/representative copy of a movie
    popularity = models.FloatField(null=True, blank=True, db_index=True)
    vote_count = models.IntegerField(null=True, blank=True)
    vote_average = models.FloatField(null=True, blank=True, db_index=True)
    imdb_id = models.CharField(max_length=20, blank=True, default='', db_index=True)
    imdb_rating = models.FloatField(null=True, blank=True, db_index=True)
    runtime = models.IntegerField(null=True, blank=True)  # in minutes
    fetched = models.BooleanField(default=False)
    metadata_status = models.CharField(max_length=32, default='missing', db_index=True)
    last_crawled = models.DateTimeField(null=True, blank=True)
    remote_modified = models.DateTimeField(null=True, blank=True)
    next_crawl = models.DateTimeField(null=True, blank=True)
    last_updated = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['title']
        indexes = [
            models.Index(fields=['group_key']),
            models.Index(fields=['clean_title', 'year']),
            models.Index(fields=['source', 'resolution']),
        ]

    def __str__(self):
        t = self.clean_title or self.title
        return f"{t} ({self.year})" if self.year else t

    @property
    def display_title(self):
        return self.clean_title or self.title

    @property
    def primary_genre(self):
        if self.genres:
            return self.genres.split(',')[0].strip()
        return ''

    @property
    def genre_list(self):
        if self.genres:
            return [g.strip() for g in self.genres.split(',') if g.strip()]
        return []

    @property
    def runtime_display(self):
        rt = self.runtime
        if not rt and hasattr(self, 'detail') and self.detail and self.detail.runtime:
            rt = self.detail.runtime
        if rt:
            h = rt // 60
            m = rt % 60
            if h > 0 and m > 0:
                return f"{h}h {m}m"
            elif h > 0:
                return f"{h}h"
            return f"{m}m"
        return ''


class CrawledDirectory(models.Model):
    """Track directories and their modification times to skip unchanged ones"""
    url = models.URLField(unique=True, db_index=True)
    remote_modified = models.DateTimeField(null=True, blank=True)
    last_crawled = models.DateTimeField(auto_now=True)
    movie_count = models.IntegerField(default=0)

    class Meta:
        verbose_name_plural = "Crawled directories"

    def __str__(self):
        return self.url


class FailedParse(models.Model):
    """Track entries that couldn't be parsed as valid movies"""
    REASON_CHOICES = [
        ('no_media', 'No media files found'),
        ('parse_error', 'Parse error'),
        ('timeout', 'Request timeout'),
        ('network_error', 'Network error'),
        ('unknown', 'Unknown error'),
    ]
    
    name = models.CharField(max_length=512)
    url = models.URLField(unique=True, db_index=True)
    reason = models.CharField(max_length=32, choices=REASON_CHOICES, default='unknown')
    raw_text = models.TextField(blank=True)
    error_message = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    retry_count = models.IntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        verbose_name_plural = "Failed parses"

    def __str__(self):
        return f"{self.name} ({self.reason})"


class MovieDetail(models.Model):
    """Extended TMDB details for future feature expansion"""
    movie = models.OneToOneField(Movie, on_delete=models.CASCADE, related_name='detail')
    tmdb_id = models.IntegerField(null=True, blank=True, db_index=True)
    overview = models.TextField(blank=True)
    genres = models.JSONField(default=list)
    runtime = models.IntegerField(null=True, blank=True)  # in minutes
    release_date = models.DateField(null=True, blank=True)
    backdrop_url = models.URLField(blank=True, null=True)
    trailer_key = models.CharField(max_length=64, blank=True, default='')  # YouTube video ID
    trailer_url = models.URLField(blank=True, null=True)
    tagline = models.CharField(max_length=512, blank=True)
    imdb_id = models.CharField(max_length=20, blank=True)
    imdb_rating = models.FloatField(null=True, blank=True)
    original_language = models.CharField(max_length=10, blank=True)
    budget = models.BigIntegerField(null=True, blank=True)
    revenue = models.BigIntegerField(null=True, blank=True)
    production_companies = models.JSONField(default=list)
    cast = models.JSONField(default=list)  # [{name, character, profile_path}, ...]
    director = models.JSONField(default=dict)  # {name, profile_path}
    fetched_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Details for {self.movie.title}"


class MediaSource(models.Model):
    """Configured media server sources (H5AI, Apache, Nginx, local NAS, Pi)"""
    name = models.CharField(max_length=100, unique=True, db_index=True)
    url = models.URLField(max_length=512)
    source_type = models.CharField(max_length=50, default='h5ai', blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_crawled = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.url})"

