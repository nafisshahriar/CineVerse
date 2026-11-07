import json
from functools import wraps
from django.conf import settings
from django.shortcuts import render, get_object_or_404
from django.db.models import Q, Min, Max, F, Count
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.core.paginator import Paginator
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.views import redirect_to_login
from collections import defaultdict
from .models import Movie, MovieDetail, CrawledDirectory, FailedParse
from .services import tmdb_service, fetch_poster_and_details
from .utils import detect_language, fetch_imdb_rating_and_id
from .task_runner import task_manager


POPULAR_GENRES = [
    'Action', 'Adventure', 'Animation', 'Comedy', 'Crime', 'Documentary',
    'Drama', 'Family', 'Fantasy', 'History', 'Horror', 'Music', 'Mystery',
    'Romance', 'Science Fiction', 'Thriller', 'War', 'Western'
]


def get_page_range(current_page: int, num_pages: int, window: int = 2) -> list:
    """Generate responsive pagination numbers with smart window and ellipsis."""
    if num_pages <= 7:
        return list(range(1, num_pages + 1))

    pages = [1]
    left = max(2, current_page - window)
    right = min(num_pages - 1, current_page + window)

    if left > 2:
        pages.append('...')
    for p in range(left, right + 1):
        pages.append(p)
    if right < num_pages - 1:
        pages.append('...')
    pages.append(num_pages)
    return pages


def get_filtered_queryset(request):
    """Reliable multi-parameter filtering & sorting logic."""
    q = request.GET.get('q', '').strip()
    year_from = request.GET.get('year_from', '').strip()
    year_to = request.GET.get('year_to', '').strip()
    min_rating = request.GET.get('min_rating', '').strip()
    sort_by = request.GET.get('sort', 'popularity').strip()
    
    # Handle genres as both list and single string
    genres_param = request.GET.getlist('genres')
    genre_single = request.GET.get('genre', '').strip()
    if genre_single and genre_single not in genres_param:
        genres_param.append(genre_single)

    language = request.GET.get('language', '').strip()
    source = request.GET.get('source', '').strip()
    resolution = request.GET.get('resolution', '').strip()
    status = request.GET.get('status', '').strip()
    group = request.GET.get('group', '1').strip()
    missing = request.GET.get('missing', '').strip()

    movies = Movie.objects.all()

    # 1. Search Query
    if q:
        movies = movies.filter(
            Q(clean_title__icontains=q) |
            Q(title__icontains=q) |
            Q(year_str__icontains=q) |
            Q(detail__director__icontains=q) |
            Q(detail__cast__icontains=q)
        )

    # 2. Source Filter
    if source:
        movies = movies.filter(source__iexact=source)

    # 3. Resolution / Quality Filter
    if resolution:
        movies = movies.filter(resolution__iexact=resolution)

    # 4. Deduplicate / Group Copies
    # If no specific source or resolution is filtered, pick 1 primary card per movie across all servers.
    # If a specific source/resolution IS chosen, do not artificially drop matching copies from that source.
    if group == '1' and not source and not resolution:
        movies = movies.filter(is_primary_copy=True)

    # 5. Language Filter
    if language:
        movies = movies.filter(language__iexact=language)

    # 6. Year Range Filter
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

    # 7. Rating Filter (Strictly authentic IMDb rating)
    if min_rating:
        try:
            mr = float(min_rating)
            movies = movies.filter(imdb_rating__gte=mr)
        except ValueError:
            pass

    # 8. Genre Filter
    # 8. Genre Filter (checks Movie.genres, MovieDetail.genres, and directory paths)
    if genres_param:
        for g in genres_param:
            if g:
                if g.lower() in ('science fiction', 'sci-fi', 'scifi'):
                    movies = movies.filter(
                        Q(genres__icontains='Science Fiction') |
                        Q(genres__icontains='Sci-Fi') |
                        Q(detail__genres__icontains='Science Fiction') |
                        Q(detail__genres__icontains='Sci-Fi') |
                        Q(directory_url__icontains='Sci-Fi') |
                        Q(directory_url__icontains='Science Fiction')
                    )
                else:
                    movies = movies.filter(
                        Q(genres__icontains=g) |
                        Q(detail__genres__icontains=g) |
                        Q(directory_url__icontains=g)
                    )

    # 9. Metadata Status Filter
    if status:
        movies = movies.filter(metadata_status=status)

    # 10. Gap / Missing Data Filter (Posters, IMDb, Genres)
    if missing == 'poster':
        movies = movies.filter(Q(poster_url__isnull=True) | Q(poster_url=''))
    elif missing == 'imdb':
        movies = movies.filter(imdb_rating__isnull=True)
    elif missing == 'genres':
        movies = movies.filter(Q(genres__isnull=True) | Q(genres=''))

    # 11. Reliable Sorting (IMDb Rating first for highest rated)
    if sort_by == 'rating':
        # Highest Rated: movies with real high IMDb ratings, then high vote count
        movies = movies.order_by(
            F('imdb_rating').desc(nulls_last=True),
            F('vote_count').desc(nulls_last=True),
            F('vote_average').desc(nulls_last=True)
        )
    elif sort_by == '-rating':
        movies = movies.order_by(
            F('imdb_rating').asc(nulls_last=True),
            F('vote_average').asc(nulls_last=True)
        )
    elif sort_by == 'popularity':
        movies = movies.order_by(F('popularity').desc(nulls_last=True))
    elif sort_by == 'votes':
        movies = movies.order_by(F('vote_count').desc(nulls_last=True))
    elif sort_by == '-year':
        movies = movies.order_by(F('year').desc(nulls_last=True))
    elif sort_by == 'year':
        movies = movies.order_by(F('year').asc(nulls_last=True))
    elif sort_by == 'title':
        movies = movies.order_by(F('clean_title').asc(nulls_last=True))
    elif sort_by == '-title':
        movies = movies.order_by(F('clean_title').desc(nulls_last=True))
    else:
        movies = movies.order_by(F('popularity').desc(nulls_last=True))

    filters = {
        'year_from': year_from,
        'year_to': year_to,
        'min_rating': min_rating,
        'sort': sort_by,
        'genres': genres_param,
        'language': language,
        'source': source,
        'resolution': resolution,
        'status': status,
        'group': group,
        'missing': missing,
    }

    return movies, filters, q


def attach_copies_to_page(movies_page):
    """Attach available versions/copies to each movie card on the page."""
    group_keys = [m.group_key for m in movies_page if m.group_key]
    copies_by_group = defaultdict(list)

    if group_keys:
        all_copies = Movie.objects.filter(group_key__in=group_keys).order_by('source', '-resolution')
        for c in all_copies:
            copies_by_group[c.group_key].append(c)

    for m in movies_page:
        m.all_copies = copies_by_group.get(m.group_key, [m])
        m.copy_count = len(m.all_copies)


def index(request):
    """Main movie listing view with rich filters, copy grouping, and pagination."""
    movies, filters, q = get_filtered_queryset(request)
    page = request.GET.get('page', 1)

    paginator = Paginator(movies, 48)
    movies_page = paginator.get_page(page)
    attach_copies_to_page(movies_page)

    year_stats = Movie.objects.exclude(year__isnull=True).aggregate(
        min_year=Min('year'),
        max_year=Max('year')
    )
    min_year = year_stats['min_year'] or 1920
    max_year = year_stats['max_year'] or 2026
    years = list(range(max_year, min_year - 1, -1))

    languages = [
        'English', 'Hindi', 'Korean', 'Japanese', 'Chinese', 'French',
        'Spanish', 'German', 'Italian', 'Russian', 'Turkish', 'Persian',
        'Indonesian', 'Thai', 'Tamil', 'Telugu', 'Malayalam', 'Bengali'
    ]

    sources = ['Dhaka-Flix 14', 'Dhaka-Flix 7', 'E-Box']

    resolutions = [
        ('4K', '4K UHD'),
        ('1080P', '1080p FHD'),
        ('720P', '720p HD'),
        ('480P', '480p SD'),
    ]

    page_range = get_page_range(movies_page.number, paginator.num_pages)

    missing_posters_count = Movie.objects.filter(is_primary_copy=True).filter(Q(poster_url__isnull=True) | Q(poster_url='')).count()
    missing_imdb_count = Movie.objects.filter(is_primary_copy=True, imdb_rating__isnull=True).count()

    context = {
        'movies': movies_page,
        'query': q,
        'years': years,
        'filters': filters,
        'total_count': paginator.count,
        'collection_count': Movie.objects.count(),
        'unique_count': Movie.objects.filter(is_primary_copy=True).count(),
        'missing_posters_count': missing_posters_count,
        'missing_imdb_count': missing_imdb_count,
        'page_range': page_range,
        'rating_options': [
            ('9', 'IMDb 9.0+ ⭐'),
            ('8', 'IMDb 8.0+ ⭐'),
            ('7', 'IMDb 7.0+ ⭐'),
            ('6', 'IMDb 6.0+ ⭐'),
            ('5', 'IMDb 5.0+ ⭐'),
        ],
        'sort_options': [
            ('popularity', '🔥 Most Popular'),
            ('rating', '⭐ Highest Rated (IMDb)'),
            ('votes', '👍 Most Votes'),
            ('-year', '📅 Newest First'),
            ('year', '📅 Oldest First'),
            ('title', '🔤 Title A-Z'),
            ('-title', '🔤 Title Z-A'),
        ],
        'language_options': languages,
        'source_options': sources,
        'resolution_options': resolutions,
        'genre_options': POPULAR_GENRES,
        'status_options': [
            ('', 'All Status'),
            ('ok', 'Found'),
            ('missing', 'Missing'),
            ('failed', 'Failed'),
        ],
        'manage_require_auth': getattr(settings, 'MANAGE_REQUIRE_AUTH', False),
    }

    return render(request, 'movies/index.html', context)


def search_ajax(request):
    """AJAX endpoint for instant search, filtering, and page navigation."""
    movies, filters, q = get_filtered_queryset(request)
    page = request.GET.get('page', 1)

    paginator = Paginator(movies, 48)
    movies_page = paginator.get_page(page)
    attach_copies_to_page(movies_page)

    page_range = get_page_range(movies_page.number, paginator.num_pages)

    html = render_to_string('movies/_movie_cards.html', {
        'movies': movies_page,
        'total_count': paginator.count,
    })

    pagination_html = render_to_string('movies/_pagination.html', {
        'movies': movies_page,
        'query': q,
        'filters': filters,
        'page_range': page_range,
    })

    return JsonResponse({
        'html': html,
        'pagination_html': pagination_html,
        'count': paginator.count,
        'num_pages': paginator.num_pages,
        'current_page': movies_page.number,
    })


def movie_detail_ajax(request, movie_id):
    """Fetch extended movie details with authentic IMDb rating and all copies."""
    movie = get_object_or_404(Movie, pk=movie_id)

    try:
        detail = movie.detail
    except MovieDetail.DoesNotExist:
        detail = None

    needs_fetch = not detail or not detail.tmdb_id or (not detail.cast and not detail.director)
    if movie.imdb_id and detail and detail.imdb_id != movie.imdb_id:
        needs_fetch = True

    search_title = movie.clean_title or movie.title
    if needs_fetch:
        extended = tmdb_service.get_extended_details(
            search_title, movie.year_str or '', imdb_id=movie.imdb_id or ''
        )

        if extended:
            if not detail:
                detail = MovieDetail(movie=movie)

            # Sync official clean title, year, poster, runtime to movie if resolved
            movie_updates = {}
            if extended.get('title') and (not movie.clean_title or movie.clean_title == movie.title):
                movie.clean_title = extended['title']
                movie_updates['clean_title'] = extended['title']
            if extended.get('release_date') and len(extended['release_date']) >= 4 and extended['release_date'][:4].isdigit():
                ext_yr = int(extended['release_date'][:4])
                if not movie.year or movie.year != ext_yr:
                    movie.year = ext_yr
                    movie.year_str = str(ext_yr)
                    movie_updates['year'] = ext_yr
                    movie_updates['year_str'] = str(ext_yr)
            elif extended.get('year') and (not movie.year or movie.year != extended['year']):
                movie.year = extended['year']
                movie.year_str = str(extended['year'])
                movie_updates['year'] = extended['year']
                movie_updates['year_str'] = str(extended['year'])
            if extended.get('poster_url') and not movie.poster_url:
                movie.poster_url = extended['poster_url']
                movie_updates['poster_url'] = extended['poster_url']
            if extended.get('runtime') and not movie.runtime:
                movie.runtime = extended['runtime']
                movie_updates['runtime'] = extended['runtime']
            if movie_updates:
                movie.save(update_fields=list(movie_updates.keys()))
                if movie.group_key:
                    Movie.objects.filter(group_key=movie.group_key).update(**movie_updates)

            detail.tmdb_id = extended.get('tmdb_id')
            detail.overview = extended.get('overview', '')
            detail.genres = extended.get('genres', [])
            detail.runtime = extended.get('runtime')
            detail.backdrop_url = extended.get('backdrop_url')
            detail.tagline = extended.get('tagline', '')
            detail.imdb_id = movie.imdb_id or extended.get('imdb_id', '')
            detail.original_language = extended.get('original_language', '')
            detail.budget = extended.get('budget')
            detail.revenue = extended.get('revenue')
            detail.production_companies = extended.get('production_companies', [])
            detail.cast = extended.get('cast', [])
            detail.director = extended.get('director', {})
            if extended.get('trailer_key'):
                detail.trailer_key = extended.get('trailer_key')
                detail.trailer_url = extended.get('trailer_url')

            if extended.get('release_date'):
                try:
                    from datetime import datetime
                    detail.release_date = datetime.strptime(
                        str(extended['release_date'])[:10], '%Y-%m-%d'
                    ).date()
                except ValueError:
                    pass

            detail.save()

            if extended.get('runtime') and not movie.runtime:
                movie.runtime = extended.get('runtime')
                movie.save(update_fields=['runtime'])

    # On-demand trailer resolution if missing
    if detail and not detail.trailer_key and detail.tmdb_id:
        try:
            trailer_info = tmdb_service.get_movie_trailer(detail.tmdb_id)
            if trailer_info and trailer_info.get('trailer_key'):
                detail.trailer_key = trailer_info['trailer_key']
                detail.trailer_url = trailer_info['trailer_url']
                detail.save(update_fields=['trailer_key', 'trailer_url'])
        except Exception:
            pass

    # Ensure movie runtime is synced from detail
    if detail and detail.runtime and not movie.runtime:
        movie.runtime = detail.runtime
        movie.save(update_fields=['runtime'])

    # Sync genres to Movie and copies in group
    if not movie.genres and detail and detail.genres:
        g_str = ', '.join([g for g in detail.genres if isinstance(g, str)])
        if g_str:
            movie.genres = g_str
            if movie.group_key:
                Movie.objects.filter(group_key=movie.group_key).update(genres=g_str)
            else:
                Movie.objects.filter(id=movie.id).update(genres=g_str)

    # Ensure authentic IMDb rating and ID
    if not movie.imdb_rating:
        known_id = movie.imdb_id or (detail.imdb_id if detail else '')
        res = fetch_imdb_rating_and_id(search_title, movie.year, known_imdb_id=known_id)
        rate = res[0]
        i_id = res[1] if len(res) > 1 else None
        g_list = res[2] if len(res) > 2 else []
        if rate:
            update_data = {'imdb_rating': rate}
            if i_id:
                update_data['imdb_id'] = i_id
            if g_list and not movie.genres:
                update_data['genres'] = ', '.join(g_list)
                movie.genres = update_data['genres']
            if movie.group_key:
                Movie.objects.filter(group_key=movie.group_key).update(**update_data)
            else:
                Movie.objects.filter(id=movie.id).update(**update_data)
            movie.imdb_rating = rate
            if i_id:
                movie.imdb_id = i_id
            if detail:
                detail.imdb_rating = rate
                if i_id:
                    detail.imdb_id = i_id
                if g_list and not detail.genres:
                    detail.genres = g_list
                detail.save()

    # Fetch all copies sharing the same group_key
    all_copies = []
    if movie.group_key:
        copies_qs = Movie.objects.filter(group_key=movie.group_key).order_by('source', '-resolution')
    else:
        copies_qs = [movie]

    for c in copies_qs:
        all_copies.append({
            'id': c.id,
            'title': c.display_title,
            'file_url': c.file_url,
            'source': c.source or 'Local',
            'resolution': c.resolution or 'HD',
            'quality': c.quality or '',
            'language': c.language or '',
        })

    imdb_code = movie.imdb_id or (detail.imdb_id if detail else '')
    genres_data = detail.genres if (detail and detail.genres) else movie.genre_list

    # Resolve authentic language strictly from TMDB/IMDb
    tmdb_lang = None
    if detail and detail.original_language:
        from movies.utils import TMDB_LANG_MAP
        code = detail.original_language.lower().strip()
        tmdb_lang = TMDB_LANG_MAP.get(code, detail.original_language.upper())

    return JsonResponse({
        'success': True,
        'id': movie.id,
        'title': movie.display_title,
        'year': movie.year,
        'poster_url': movie.poster_url,
        'tmdb_id': detail.tmdb_id if detail else None,
        'imdb_id': imdb_code,
        'imdb_rating': movie.imdb_rating or (detail.imdb_rating if detail else None),
        'overview': detail.overview if detail else '',
        'genres': genres_data,
        'runtime': (detail.runtime if detail else None) or movie.runtime,
        'release_date': str(detail.release_date) if (detail and detail.release_date) else None,
        'backdrop_url': detail.backdrop_url if detail else '',
        'tagline': detail.tagline if detail else '',
        'original_language': detail.original_language if detail else None,
        'language': tmdb_lang,  # Strictly TMDB/IMDb authentic language name
        'trailer_key': detail.trailer_key if (detail and detail.trailer_key) else None,
        'trailer_url': detail.trailer_url if (detail and detail.trailer_url) else None,
        'budget': detail.budget if detail else None,
        'revenue': detail.revenue if detail else None,
        'production_companies': detail.production_companies if detail else [],
        'cast': detail.cast if detail else [],
        'director': detail.director if detail else {},
        'vote_average': movie.vote_average,
        'vote_count': movie.vote_count,
        'popularity': movie.popularity,
        'copies': all_copies,
        'source': movie.source,
        'resolution': movie.resolution,
        'can_manage': bool(not getattr(settings, 'MANAGE_REQUIRE_AUTH', False) or (request.user.is_authenticated and request.user.is_staff)),
    })


@csrf_exempt
def movie_refresh_ajax(request, movie_id):
    """
    Public on-demand metadata & IMDb rating fetch/refresh for a single movie.
    Safe, non-destructive, and accessible without administrator login.
    Enriches TMDB poster, backdrop, runtime, trailer, and authentic IMDb rating.
    Accepts optional {imdb_id, year} in POST body to override before fetching.
    """
    if request.method not in ('POST', 'GET'):
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    movie = get_object_or_404(Movie, pk=movie_id)

    # Allow optional manual imdb_id or year passed in request body
    if request.body:
        try:
            body_data = json.loads(request.body.decode('utf-8'))
            from movies.utils import normalize_imdb_id
            raw_id = body_data.get('imdb_id', '').strip()
            clean_id = normalize_imdb_id(raw_id) if raw_id else ''
            if clean_id:
                movie.imdb_id = clean_id
                movie.save(update_fields=['imdb_id'])
                if movie.group_key:
                    Movie.objects.filter(group_key=movie.group_key).update(imdb_id=clean_id)
            if body_data.get('year'):
                try:
                    y = int(body_data['year'])
                    movie.year = y
                    movie.year_str = str(y)
                    movie.save(update_fields=['year', 'year_str'])
                    if movie.group_key:
                        Movie.objects.filter(group_key=movie.group_key).update(year=y, year_str=str(y))
                except (ValueError, TypeError):
                    pass
        except Exception:
            pass

    from movies.services import enrich_movie_metadata
    success, res_data = enrich_movie_metadata(movie, include_details=True)

    if success:
        return JsonResponse({
            'success': True,
            'message': f"Enriched: {movie.display_title}",
            'movie': res_data,
        })
    else:
        return JsonResponse({
            'success': False,
            'message': res_data.get('error', 'Could not resolve movie from TMDB or IMDb'),
            'movie': {
                'id': movie.id,
                'title': movie.display_title,
                'poster_url': movie.poster_url,
                'imdb_rating': movie.imdb_rating,
                'runtime': movie.runtime,
                'genres': movie.genres,
                'metadata_status': movie.metadata_status,
            }
        })


# ============================================================
# Management & Maintenance Access Control & Views
# ============================================================

def manage_auth_required(view_func):
    """
    Access control decorator for the Management & Maintenance Studio.
    If settings.MANAGE_REQUIRE_AUTH is True (default in production / DEBUG=False):
      - Verifies that request.user is authenticated and has is_staff=True.
      - Alternatively accepts X-Manage-Token header or ?token= query param if MANAGE_SECRET_TOKEN is set.
      - Blocks unauthorized visitors:
        - API endpoints return HTTP 403 Forbidden with JSON error message.
        - Web pages redirect to /admin/login/?next=<path>.
    If settings.MANAGE_REQUIRE_AUTH is False:
      - Allows unrestricted access (for local development or private home networks).
    """
    @wraps(view_func)
    def _wrapped(request, *args, **kwargs):
        require_auth = getattr(settings, 'MANAGE_REQUIRE_AUTH', False)
        if not require_auth:
            return view_func(request, *args, **kwargs)

        # 1. Staff session check
        if request.user.is_authenticated and request.user.is_staff:
            return view_func(request, *args, **kwargs)

        # 2. Token-based API access (for curl / cron jobs / webhooks)
        secret_token = getattr(settings, 'MANAGE_SECRET_TOKEN', '')
        if secret_token:
            token = request.headers.get('X-Manage-Token') or request.GET.get('token')
            if token and token == secret_token:
                return view_func(request, *args, **kwargs)

        # 3. Deny unauthorized access
        if request.path.startswith('/manage/api/'):
            return JsonResponse({
                'success': False,
                'error': 'Unauthorized',
                'is_auth_error': True,
                'login_url': '/admin/login/?next=' + request.path,
                'message': 'Administrator authentication is required to access management controls. If your session expired, please log in again.'
            }, status=403)

        return redirect_to_login(request.get_full_path(), login_url='/admin/login/')

    return _wrapped


@manage_auth_required
def manage_dashboard(request):
    """Render the management & maintenance dashboard."""
    total_files = Movie.objects.count()
    unique_movies = Movie.objects.filter(is_primary_copy=True).count()

    missing_posters = Movie.objects.filter(Q(poster_url__isnull=True) | Q(poster_url='')).count()
    with_posters = total_files - missing_posters
    poster_pct = round((with_posters / total_files * 100), 1) if total_files else 0

    missing_imdb = Movie.objects.filter(imdb_rating__isnull=True).count()
    with_imdb = total_files - missing_imdb
    imdb_pct = round((with_imdb / total_files * 100), 1) if total_files else 0

    missing_genres = Movie.objects.filter(genres='').count()
    with_genres = total_files - missing_genres
    genres_pct = round((with_genres / total_files * 100), 1) if total_files else 0

    missing_clean = Movie.objects.filter(clean_title='').count()

    sources_breakdown = list(Movie.objects.values('source').annotate(count=Count('id')).order_by('-count'))
    resolutions_breakdown = list(Movie.objects.values('resolution').annotate(count=Count('id')).order_by('-count'))
    status_breakdown = list(Movie.objects.values('metadata_status').annotate(count=Count('id')).order_by('-count'))

    recent_crawls = list(CrawledDirectory.objects.order_by('-last_crawled')[:8])
    failed_count = FailedParse.objects.count()

    context = {
        'total_files': total_files,
        'unique_movies': unique_movies,
        'missing_posters': missing_posters,
        'with_posters': with_posters,
        'poster_pct': poster_pct,
        'missing_imdb': missing_imdb,
        'with_imdb': with_imdb,
        'imdb_pct': imdb_pct,
        'missing_genres': missing_genres,
        'with_genres': with_genres,
        'genres_pct': genres_pct,
        'missing_clean': missing_clean,
        'sources_breakdown': sources_breakdown,
        'resolutions_breakdown': resolutions_breakdown,
        'status_breakdown': status_breakdown,
        'recent_crawls': recent_crawls,
        'failed_count': failed_count,
        'task_status': task_manager.get_status(),
    }
    return render(request, 'movies/manage.html', context)


@manage_auth_required
def manage_stats_api(request):
    """API returning live metrics and progress for the management dashboard."""
    total_files = Movie.objects.count()
    unique_movies = Movie.objects.filter(is_primary_copy=True).count()
    missing_posters = Movie.objects.filter(Q(poster_url__isnull=True) | Q(poster_url='')).count()
    missing_imdb = Movie.objects.filter(imdb_rating__isnull=True).count()
    missing_genres = Movie.objects.filter(genres='').count()
    missing_clean = Movie.objects.filter(clean_title='').count()

    return JsonResponse({
        'total_files': total_files,
        'unique_movies': unique_movies,
        'with_posters': total_files - missing_posters,
        'missing_posters': missing_posters,
        'poster_pct': round(((total_files - missing_posters) / total_files * 100), 1) if total_files else 0,
        'with_imdb': total_files - missing_imdb,
        'missing_imdb': missing_imdb,
        'imdb_pct': round(((total_files - missing_imdb) / total_files * 100), 1) if total_files else 0,
        'with_genres': total_files - missing_genres,
        'missing_genres': missing_genres,
        'genres_pct': round(((total_files - missing_genres) / total_files * 100), 1) if total_files else 0,
        'missing_clean': missing_clean,
        'task_running': task_manager.is_running(),
    })


@csrf_exempt
@manage_auth_required
def manage_task_start_api(request):
    """API endpoint to launch maintenance background tasks."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST.dict()

    task_type = data.get('task_type')
    params = data.get('params', {})
    if not task_type:
        return JsonResponse({'success': False, 'message': 'Task type is required'}, status=400)

    success, msg = task_manager.start_task(task_type, params)
    return JsonResponse({'success': success, 'message': msg})


def manage_task_status_api(request):
    """API endpoint to poll active background task progress and streaming logs."""
    return JsonResponse(task_manager.get_status())


@csrf_exempt
@manage_auth_required
def manage_task_stop_api(request):
    """API endpoint to cancel / stop an active background task."""
    success, msg = task_manager.stop_task()
    return JsonResponse({'success': success, 'message': msg})


@manage_auth_required
def manage_movies_api(request):
    """API endpoint for paginated and issue-filtered movie inspection."""
    q = request.GET.get('q', '').strip()
    issue = request.GET.get('issue', '').strip()
    source = request.GET.get('source', '').strip()
    try:
        page = int(request.GET.get('page', 1))
    except ValueError:
        page = 1

    qs = Movie.objects.all()
    if q:
        qs = qs.filter(Q(clean_title__icontains=q) | Q(title__icontains=q) | Q(year_str__icontains=q) | Q(imdb_id__icontains=q))

    if source:
        qs = qs.filter(source__iexact=source)

    if issue == 'missing_poster':
        qs = qs.filter(Q(poster_url__isnull=True) | Q(poster_url=''))
    elif issue == 'missing_imdb':
        qs = qs.filter(imdb_rating__isnull=True)
    elif issue == 'missing_genres':
        qs = qs.filter(genres='')
    elif issue == 'missing_clean':
        qs = qs.filter(clean_title='')
    elif issue == 'failed':
        qs = qs.filter(metadata_status='failed')
    elif issue == 'missing_meta':
        qs = qs.filter(metadata_status='missing')

    qs = qs.order_by('-id')

    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(page)

    movies_data = []
    for m in page_obj:
        movies_data.append({
            'id': m.id,
            'title': m.display_title,
            'raw_title': m.title,
            'year': m.year,
            'source': m.source,
            'resolution': m.resolution,
            'poster_url': m.poster_url,
            'imdb_id': m.imdb_id,
            'imdb_rating': m.imdb_rating,
            'genres': m.genres,
            'metadata_status': m.metadata_status,
            'is_primary': m.is_primary_copy,
            'file_url': m.file_url,
        })

    return JsonResponse({
        'movies': movies_data,
        'total': paginator.count,
        'num_pages': paginator.num_pages,
        'current_page': page_obj.number,
    })


@csrf_exempt
def manage_movie_action_api(request, movie_id):
    """API endpoint for single-movie maintenance actions (re-fetch, edit IDs, delete)."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    movie = get_object_or_404(Movie, pk=movie_id)
    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST.dict()

    action = data.get('action')

    # Safe on-demand metadata & IMDb rating fetch/refresh: accessible without staff auth
    if action == 'refresh':
        from movies.services import enrich_movie_metadata
        success, res_data = enrich_movie_metadata(movie, include_details=True)
        if success:
            return JsonResponse({
                'success': True,
                'message': f"Enriched: {movie.display_title}",
                'movie': res_data,
            })
        else:
            return JsonResponse({
                'success': False,
                'message': res_data.get('error', 'Could not resolve movie from TMDB or IMDb'),
                'movie': {
                    'id': movie.id,
                    'title': movie.display_title,
                    'poster_url': movie.poster_url,
                    'imdb_rating': movie.imdb_rating,
                    'runtime': movie.runtime,
                    'genres': movie.genres,
                    'metadata_status': movie.metadata_status,
                }
            })

    # Destructive actions (update_ids, delete) require staff authentication
    require_auth = getattr(settings, 'MANAGE_REQUIRE_AUTH', False)
    if require_auth and not (request.user.is_authenticated and request.user.is_staff):
        secret_token = getattr(settings, 'MANAGE_SECRET_TOKEN', '')
        token = request.headers.get('X-Manage-Token') or request.GET.get('token')
        if not (secret_token and token == secret_token):
            return JsonResponse({
                'success': False,
                'error': 'Unauthorized',
                'is_auth_error': True,
                'login_url': '/admin/login/?next=' + request.path,
                'message': 'Administrator authentication is required to edit or delete movies.'
            }, status=403)

    if action == 'update_ids':
        from movies.utils import normalize_imdb_id
        raw_imdb = data.get('imdb_id', '').strip()
        clean_imdb = normalize_imdb_id(raw_imdb) if raw_imdb else ''
        imdb_id = clean_imdb or raw_imdb
        imdb_rating = data.get('imdb_rating')
        clean_title = data.get('clean_title', '').strip()
        year = data.get('year')
        genres = data.get('genres', '').strip()
        poster_url = data.get('poster_url', '').strip()

        group_updates = {}
        if imdb_id:
            movie.imdb_id = imdb_id
            group_updates['imdb_id'] = imdb_id
        if imdb_rating is not None and imdb_rating != '':
            try:
                movie.imdb_rating = float(imdb_rating)
                group_updates['imdb_rating'] = movie.imdb_rating
            except ValueError:
                pass
        if clean_title:
            movie.clean_title = clean_title
            group_updates['clean_title'] = clean_title
        if year:
            try:
                movie.year = int(year)
                movie.year_str = str(year)
                group_updates['year'] = movie.year
                group_updates['year_str'] = movie.year_str
            except ValueError:
                pass
        if genres:
            movie.genres = genres
            group_updates['genres'] = genres
        if poster_url:
            movie.poster_url = poster_url
            movie.metadata_status = 'ok'
            group_updates['poster_url'] = poster_url
            group_updates['metadata_status'] = 'ok'

        movie.save()
        if movie.group_key and group_updates:
            Movie.objects.filter(group_key=movie.group_key).update(**group_updates)

        if data.get('refetch'):
            from movies.services import enrich_movie_metadata
            enrich_movie_metadata(movie, include_details=True)
            movie.refresh_from_db()

        return JsonResponse({
            'success': True,
            'message': f"Updated {movie.display_title}",
            'movie': {
                'id': movie.id,
                'title': movie.display_title,
                'year': movie.year,
                'imdb_id': movie.imdb_id,
                'imdb_rating': movie.imdb_rating,
                'poster_url': movie.poster_url,
                'genres': movie.genres,
            }
        })

    elif action == 'delete':
        title = movie.display_title
        movie.delete()
        return JsonResponse({'success': True, 'message': f"Deleted movie: {title}"})

    return JsonResponse({'success': False, 'message': f"Unknown action: {action}"})
