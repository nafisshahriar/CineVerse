from django.shortcuts import render, get_object_or_404
from django.db.models import Q, Min, Max
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.core.paginator import Paginator
from .models import Movie, MovieDetail
from .services import tmdb_service


def index(request):
    """Main movie listing view with search, filters, and pagination"""
    # Get filter parameters
    q = request.GET.get('q', '').strip()
    year_from = request.GET.get('year_from', '')
    year_to = request.GET.get('year_to', '')
    min_rating = request.GET.get('min_rating', '')
    sort_by = request.GET.get('sort', '-popularity')
    genres_param = request.GET.getlist('genres')  # Multi-select genres
    page = request.GET.get('page', 1)
    
    # Start with all movies
    movies = Movie.objects.all()
    
    # Apply search filter
    if q:
        movies = movies.filter(
            Q(title__icontains=q) | 
            Q(year_str__icontains=q)
        )
    
    # Apply year range filter
    if year_from:
        try:
            movies = movies.filter(year__gte=int(year_from))
        except ValueError:
            pass
    
    if year_to:
        try:
            movies = movies.filter(year__lte=int(year_to))
        except ValueError:
            pass
    
    # Apply rating filter
    if min_rating:
        try:
            movies = movies.filter(vote_average__gte=float(min_rating))
        except ValueError:
            pass
    
    # Apply genre filter (SQLite-compatible using icontains on JSON text)
    if genres_param:
        for genre in genres_param:
            # Use icontains on the genres field - works as genres is stored as JSON array text
            movies = movies.filter(detail__genres__icontains=genre)
    
    # Apply sorting
    sort_options = {
        'title': 'title',
        '-title': '-title',
        'year': 'year',
        '-year': '-year',
        'rating': '-vote_average',
        '-rating': 'vote_average',
        'popularity': '-popularity',
        '-popularity': 'popularity',
        'votes': '-vote_count',
    }
    order_field = sort_options.get(sort_by, '-popularity')
    
    # Handle null values in sorting using F() with nulls_last
    from django.db.models import F
    if order_field.startswith('-'):
        field_name = order_field[1:]
        movies = movies.order_by(F(field_name).desc(nulls_last=True))
    else:
        movies = movies.order_by(F(order_field).asc(nulls_last=True))
    
    # Pagination
    paginator = Paginator(movies, 48)
    movies_page = paginator.get_page(page)
    
    # Get year range for filter dropdowns
    year_stats = Movie.objects.exclude(year__isnull=True).aggregate(
        min_year=Min('year'),
        max_year=Max('year')
    )
    min_year = year_stats['min_year'] or 1900
    max_year = year_stats['max_year'] or 2024
    years = list(range(max_year, min_year - 1, -1))
    
    # Get all unique genres for the multi-select dropdown
    all_genres = set()
    for detail in MovieDetail.objects.exclude(genres=[]).values_list('genres', flat=True):
        if detail:
            all_genres.update(detail)
    genre_options = sorted(all_genres)
    
    # Build filter context
    filters = {
        'year_from': year_from,
        'year_to': year_to,
        'min_rating': min_rating,
        'sort': sort_by,
        'genres': genres_param,
    }
    
    context = {
        'movies': movies_page,
        'query': q,
        'years': years,
        'filters': filters,
        'total_count': paginator.count,
        'collection_count': Movie.objects.count(),  # Total unfiltered count
        'rating_options': [
            ('9', '9+ ⭐'),
            ('8', '8+ ⭐'),
            ('7', '7+ ⭐'),
            ('6', '6+ ⭐'),
            ('5', '5+ ⭐'),
        ],
        'sort_options': [
            ('popularity', '🔥 Most Popular'),
            ('rating', '⭐ Highest Rated'),
            ('votes', '👍 Most Votes'),
            ('-year', '📅 Newest First'),
            ('year', '📅 Oldest First'),
            ('title', '🔤 Title A-Z'),
            ('-title', '🔤 Title Z-A'),
        ],
        'genre_options': genre_options,
    }
    
    return render(request, 'movies/index.html', context)


def search_ajax(request):
    """AJAX endpoint for live search"""
    q = request.GET.get('q', '').strip()
    year_from = request.GET.get('year_from', '')
    year_to = request.GET.get('year_to', '')
    min_rating = request.GET.get('min_rating', '')
    sort_by = request.GET.get('sort', '-popularity')
    genres_param = request.GET.getlist('genres')  # Multi-select genres
    page = request.GET.get('page', 1)
    
    movies = Movie.objects.all()
    
    if q:
        movies = movies.filter(
            Q(title__icontains=q) | 
            Q(year_str__icontains=q)
        )
    
    if year_from:
        try:
            movies = movies.filter(year__gte=int(year_from))
        except ValueError:
            pass
    
    if year_to:
        try:
            movies = movies.filter(year__lte=int(year_to))
        except ValueError:
            pass
    
    if min_rating:
        try:
            movies = movies.filter(vote_average__gte=float(min_rating))
        except ValueError:
            pass
    
    # Apply genre filter (SQLite-compatible using icontains on JSON text)
    if genres_param:
        for genre in genres_param:
            movies = movies.filter(detail__genres__icontains=genre)
    
    sort_options = {
        'title': 'title',
        '-title': '-title',
        'year': 'year',
        '-year': '-year',
        'rating': '-vote_average',
        'popularity': '-popularity',
        'votes': '-vote_count',
    }
    order_field = sort_options.get(sort_by, '-popularity')
    
    from django.db.models import F
    if order_field.startswith('-'):
        field_name = order_field[1:]
        movies = movies.order_by(F(field_name).desc(nulls_last=True))
    else:
        movies = movies.order_by(F(order_field).asc(nulls_last=True))
    
    paginator = Paginator(movies, 48)
    movies_page = paginator.get_page(page)
    
    html = render_to_string('movies/_movie_cards.html', {
        'movies': movies_page,
        'total_count': paginator.count,
    })
    
    # Return JSON with HTML and count for the results counter
    return JsonResponse({
        'html': html,
        'count': paginator.count,
    })


def movie_detail_ajax(request, movie_id):
    """Fetch extended movie details from TMDB"""
    movie = get_object_or_404(Movie, pk=movie_id)
    
    # Try to get existing details
    try:
        detail = movie.detail
    except MovieDetail.DoesNotExist:
        detail = None
    
    # Fetch from TMDB if needed (no detail, no tmdb_id, or missing cast/director)
    needs_fetch = not detail or not detail.tmdb_id or (not detail.cast and not detail.director)
    
    if needs_fetch:
        extended = tmdb_service.get_extended_details(movie.title, movie.year_str or '')
        
        if extended:
            if not detail:
                detail = MovieDetail(movie=movie)
            
            detail.tmdb_id = extended.get('tmdb_id')
            detail.overview = extended.get('overview', '')
            detail.genres = extended.get('genres', [])
            detail.runtime = extended.get('runtime')
            detail.backdrop_url = extended.get('backdrop_url')
            detail.tagline = extended.get('tagline', '')
            detail.imdb_id = extended.get('imdb_id', '')
            detail.original_language = extended.get('original_language', '')
            detail.budget = extended.get('budget')
            detail.revenue = extended.get('revenue')
            detail.production_companies = extended.get('production_companies', [])
            detail.cast = extended.get('cast', [])
            detail.director = extended.get('director', {})
            
            if extended.get('release_date'):
                try:
                    from datetime import datetime
                    detail.release_date = datetime.strptime(
                        extended['release_date'], '%Y-%m-%d'
                    ).date()
                except ValueError:
                    pass
            
            detail.save()
    
    if detail:
        return JsonResponse({
            'success': True,
            'tmdb_id': detail.tmdb_id,
            'overview': detail.overview,
            'genres': detail.genres,
            'runtime': detail.runtime,
            'release_date': str(detail.release_date) if detail.release_date else None,
            'backdrop_url': detail.backdrop_url,
            'tagline': detail.tagline,
            'imdb_id': detail.imdb_id,
            'production_companies': detail.production_companies,
            'cast': detail.cast,
            'director': detail.director,
            # Movie stats from parent Movie model
            'year': movie.year,
            'vote_average': movie.vote_average,
            'vote_count': movie.vote_count,
            'popularity': movie.popularity,
        })
    
    return JsonResponse({
        'success': False,
        'error': 'Could not fetch movie details'
    })
