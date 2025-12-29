from django.core.management.base import BaseCommand
from movies.models import Movie, MovieDetail
from movies.services import tmdb_service


class Command(BaseCommand):
    help = 'Fetch cast and director credits for movies from TMDB'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Re-fetch credits for all movies')
        parser.add_argument('--movie-id', type=int, help='Fetch for a specific movie ID')
        parser.add_argument('--limit', type=int, default=None, help='Limit number of movies to process')

    def handle(self, *args, **options):
        fetch_all = options['all']
        movie_id = options['movie_id']
        limit = options['limit']
        
        if movie_id:
            # Fetch for specific movie
            try:
                movie = Movie.objects.get(pk=movie_id)
                self._fetch_credits_for_movie(movie, force=True)
            except Movie.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'Movie with ID {movie_id} not found'))
            return
        
        # Build queryset
        if fetch_all:
            # All movies with TMDB ID
            movies = Movie.objects.filter(detail__tmdb_id__isnull=False)
            self.stdout.write(self.style.MIGRATE_HEADING('Fetching credits for ALL movies...'))
        else:
            # Only movies missing cast/director
            movies = Movie.objects.filter(
                detail__tmdb_id__isnull=False,
                detail__cast=[]
            )
            self.stdout.write(self.style.MIGRATE_HEADING('Fetching credits for movies missing cast data...'))
        
        if limit:
            movies = movies[:limit]
        
        total = movies.count()
        self.stdout.write(f'Found {total} movies to process')
        
        success = 0
        failed = 0
        
        for i, movie in enumerate(movies, 1):
            result = self._fetch_credits_for_movie(movie, force=fetch_all)
            if result:
                success += 1
            else:
                failed += 1
            
            if i % 10 == 0:
                self.stdout.write(f'  Progress: {i}/{total} processed...')
        
        self.stdout.write(self.style.SUCCESS(f'\nDone! Success: {success}, Failed: {failed}'))

    def _fetch_credits_for_movie(self, movie: Movie, force: bool = False) -> bool:
        """Fetch and save credits for a single movie"""
        try:
            detail = movie.detail
        except MovieDetail.DoesNotExist:
            self.stdout.write(self.style.WARNING(f'No detail for: {movie.title}'))
            return False
        
        if not detail.tmdb_id:
            self.stdout.write(self.style.WARNING(f'No TMDB ID for: {movie.title}'))
            return False
        
        # Skip if already has credits and not forcing
        if not force and detail.cast:
            return True
        
        try:
            credits_data = tmdb_service._extract_credits(detail.tmdb_id)
            detail.cast = credits_data.get('cast', [])
            detail.director = credits_data.get('director', {})
            detail.save()
            
            director_name = detail.director.get('name', 'Unknown') if detail.director else 'Unknown'
            self.stdout.write(self.style.SUCCESS(
                f'Fetched: {movie.title} - Director: {director_name}, Cast: {len(detail.cast)}'
            ))
            return True
            
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error fetching {movie.title}: {e}'))
            return False
