from django.core.management.base import BaseCommand
from movies.models import Movie, MovieDetail
from movies.services import tmdb_service


class Command(BaseCommand):
    help = 'Refresh TMDB metadata (ratings, popularity, votes) for existing movies'

    def add_arguments(self, parser):
        parser.add_argument('--all', action='store_true', help='Refresh all movies (default: only fetched movies)')
        parser.add_argument('--movie-id', type=int, help='Refresh a specific movie by ID')
        parser.add_argument('--limit', type=int, default=None, help='Limit number of movies to process')

    def handle(self, *args, **options):
        movie_id = options['movie_id']
        limit = options['limit']
        
        if movie_id:
            try:
                movie = Movie.objects.get(pk=movie_id)
                self._refresh_movie(movie)
            except Movie.DoesNotExist:
                self.stdout.write(self.style.ERROR(f'Movie with ID {movie_id} not found'))
            return
        
        # Get movies with TMDB data
        movies = Movie.objects.filter(fetched=True, detail__tmdb_id__isnull=False)
        
        if limit:
            movies = movies[:limit]
        
        total = movies.count()
        self.stdout.write(self.style.MIGRATE_HEADING(f'Refreshing metadata for {total} movies...'))
        
        success = 0
        failed = 0
        
        for i, movie in enumerate(movies, 1):
            if self._refresh_movie(movie):
                success += 1
            else:
                failed += 1
            
            if i % 20 == 0:
                self.stdout.write(f'  Progress: {i}/{total}...')
        
        self.stdout.write(self.style.SUCCESS(f'\nDone! Updated: {success}, Failed: {failed}'))

    def _refresh_movie(self, movie: Movie) -> bool:
        """Refresh metadata for a single movie"""
        try:
            detail = movie.detail
            if not detail or not detail.tmdb_id:
                self.stdout.write(self.style.WARNING(f'No TMDB ID: {movie.title}'))
                return False
            
            # Fetch fresh details from TMDB
            details = tmdb_service.get_movie_details(detail.tmdb_id)
            
            if details:
                # Update volatile fields
                old_rating = movie.vote_average
                movie.popularity = details.get('popularity') or movie.popularity
                movie.vote_count = details.get('vote_count') or movie.vote_count
                movie.vote_average = details.get('vote_average') or movie.vote_average
                movie.save()
                
                rating_change = ''
                if old_rating and movie.vote_average:
                    diff = movie.vote_average - old_rating
                    if abs(diff) > 0.01:
                        rating_change = f' (rating: {old_rating:.1f} -> {movie.vote_average:.1f})'
                
                self.stdout.write(self.style.SUCCESS(f'Updated: {movie.title}{rating_change}'))
                return True
            else:
                self.stdout.write(self.style.WARNING(f'No data from TMDB: {movie.title}'))
                return False
                
        except Exception as e:
            self.stdout.write(self.style.ERROR(f'Error: {movie.title} - {e}'))
            return False
