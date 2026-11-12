import json
from functools import wraps
from django.conf import settings
from django.core.cache import cache
from django.shortcuts import render, get_object_or_404
from django.db import connection
from django.db.models import Q, Min, Max, F, Count, Subquery
from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.views import redirect_to_login
from collections import defaultdict
from .models import Movie, MovieDetail, CrawledDirectory, FailedParse, MediaSource
from .pagination import WindowCountPaginator
from .cache_utils import INDEX_SUMMARY_CACHE_KEY, invalidate_catalog_cache
from .services import tmdb_service, fetch_poster_and_details
from .utils import detect_language, fetch_imdb_rating_and_id, test_source_connection, normalize_crawl_url
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

    # 1. Search Query (UNION subquery enables PostgreSQL pg_trgm GIN indexes alongside MovieDetail cast/director search)
    if q:
        movie_match_q = Q(clean_title__icontains=q) | Q(title__icontains=q)
        if q.isdigit():
            movie_match_q |= Q(year_str__icontains=q)
        title_matches = Movie.objects.filter(movie_match_q).order_by().values('id')
        detail_matches = MovieDetail.objects.filter(
            Q(director__icontains=q) | Q(cast__icontains=q)
        ).order_by().values('movie_id')
        movies = movies.filter(id__in=Subquery(title_matches.union(detail_matches)))

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

    # 11. Reliable Sorting with deterministic tiebreakers across SQLite and PostgreSQL
    if sort_by == 'rating':
        # Highest Rated: movies with real high IMDb ratings, then high vote count
        movies = movies.order_by(
            F('imdb_rating').desc(nulls_last=True),
            F('vote_count').desc(nulls_last=True),
            F('vote_average').desc(nulls_last=True),
            '-id',
        )
    elif sort_by == '-rating':
        movies = movies.order_by(
            F('imdb_rating').asc(nulls_last=True),
            F('vote_average').asc(nulls_last=True),
            'id',
        )
    elif sort_by == 'popularity':
        movies = movies.order_by(F('popularity').desc(nulls_last=True), '-id')
    elif sort_by == 'votes':
        movies = movies.order_by(F('vote_count').desc(nulls_last=True), '-id')
    elif sort_by == '-year':
        movies = movies.order_by(
            F('year').desc(nulls_last=True),
            F('last_crawled').desc(nulls_last=True),
            '-id',
        )
    elif sort_by == 'year':
        movies = movies.order_by(F('year').asc(nulls_last=True), 'id')
    elif sort_by == 'title':
        movies = movies.order_by(F('clean_title').asc(nulls_last=True), 'id')
    elif sort_by == '-title':
        movies = movies.order_by(F('clean_title').desc(nulls_last=True), '-id')
    else:
        movies = movies.order_by(F('popularity').desc(nulls_last=True), '-id')

    # Preload detail__runtime in the same JOIN to eliminate N+1 queries per movie card
    movies = movies.select_related('detail').only(
        'id', 'title', 'clean_title', 'year', 'year_str', 'poster_url',
        'file_url', 'directory_url', 'source', 'resolution', 'quality',
        'language', 'genres', 'group_key', 'is_primary_copy', 'popularity',
        'vote_count', 'vote_average', 'imdb_id', 'imdb_rating', 'runtime',
        'fetched', 'metadata_status', 'last_crawled', 'remote_modified',
        'next_crawl', 'last_updated', 'detail__id', 'detail__runtime',
    )

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


def _get_catalog_stats(include_manage: bool = False) -> dict:
    """Compute global catalog metrics in a single aggregate SQL round trip."""
    agg = dict(
        min_year=Min('year', filter=Q(year__isnull=False)),
        max_year=Max('year', filter=Q(year__isnull=False)),
        total_files=Count('id'),
        unique_movies=Count('id', filter=Q(is_primary_copy=True)),
        missing_posters_primary=Count(
            'id',
            filter=Q(is_primary_copy=True) & (Q(poster_url__isnull=True) | Q(poster_url='')),
        ),
        missing_imdb_primary=Count('id', filter=Q(is_primary_copy=True, imdb_rating__isnull=True)),
    )
    if include_manage:
        agg.update(
            missing_posters_all=Count('id', filter=Q(poster_url__isnull=True) | Q(poster_url='')),
            missing_imdb_all=Count('id', filter=Q(imdb_rating__isnull=True)),
            missing_genres_all=Count('id', filter=Q(genres='')),
            missing_clean_all=Count('id', filter=Q(clean_title='')),
        )
    return Movie.objects.aggregate(**agg)


def _get_cached_index_summary() -> dict:
    """Fetch and briefly cache homepage catalog stats and merged source options."""
    cached = cache.get(INDEX_SUMMARY_CACHE_KEY)
    if cached is not None:
        return cached

    stats = _get_catalog_stats(include_manage=False)
    # Combine distinct DB sources and configured active sources in one SQL UNION query
    combined_sources = list(
        Movie.objects.exclude(source='').order_by().values_list('source', flat=True)
        .union(MediaSource.objects.filter(is_active=True).order_by().values_list('name', flat=True))
    )
    sources_set = set(combined_sources + ['Dhaka-Flix 14', 'Dhaka-Flix 7', 'E-Box'])
    sources = sorted(list(sources_set), key=lambda x: x.lower())

    summary = {'stats': stats, 'sources': sources}
    ttl = getattr(settings, 'CATALOG_STATS_CACHE_SECONDS', 60)
    if ttl > 0:
        cache.set(INDEX_SUMMARY_CACHE_KEY, summary, ttl)
    return summary


def index(request):
    """Main movie listing view with rich filters, copy grouping, and pagination."""
    movies, filters, q = get_filtered_queryset(request)
    page = request.GET.get('page', 1)

    paginator = WindowCountPaginator(movies, 48)
    movies_page = paginator.get_page(page)

    summary = _get_cached_index_summary()
    stats = summary['stats']
    sources = summary['sources']
    min_year = stats['min_year'] or 1920
    max_year = stats['max_year'] or 2026
    years = list(range(max_year, min_year - 1, -1))

    languages = [
        'English', 'Hindi', 'Korean', 'Japanese', 'Chinese', 'French',
        'Spanish', 'German', 'Italian', 'Russian', 'Turkish', 'Persian',
        'Indonesian', 'Thai', 'Tamil', 'Telugu', 'Malayalam', 'Bengali'
    ]

    resolutions = [
        ('4K', '4K UHD'),
        ('1080P', '1080p FHD'),
        ('720P', '720p HD'),
        ('480P', '480p SD'),
    ]

    page_range = get_page_range(movies_page.number, paginator.num_pages)

    context = {
        'movies': movies_page,
        'query': q,
        'years': years,
        'filters': filters,
        'total_count': paginator.count,
        'collection_count': stats['total_files'],
        'unique_count': stats['unique_movies'],
        'missing_posters_count': stats['missing_posters_primary'],
        'missing_imdb_count': stats['missing_imdb_primary'],
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

    paginator = WindowCountPaginator(movies, 48)
    movies_page = paginator.get_page(page)

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
    movie = get_object_or_404(Movie.objects.select_related('detail'), pk=movie_id)

    try:
        detail = movie.detail
    except MovieDetail.DoesNotExist:
        detail = None

    needs_enrichment = (
        not detail
        or (not detail.cast and not detail.director)
        or (movie.imdb_id and detail and detail.imdb_id != movie.imdb_id)
    )

    attempted_enrich = False
    enrich_ok = False
    if needs_enrichment:
        from movies.services import enrich_movie_metadata
        attempted_enrich = True
        enrich_ok, _ = enrich_movie_metadata(movie, include_details=True)
        movie = Movie.objects.select_related('detail').get(pk=movie.pk)
        try:
            detail = movie.detail
        except MovieDetail.DoesNotExist:
            detail = None
        if enrich_ok:
            invalidate_catalog_cache()

    needs_fetch = (not detail or not detail.tmdb_id or (not detail.cast and not detail.director)) and not attempted_enrich

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
            if extended.get('title'):
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
            if extended.get('poster_url'):
                movie.poster_url = extended['poster_url']
                movie_updates['poster_url'] = extended['poster_url']
            if extended.get('runtime'):
                movie.runtime = extended['runtime']
                movie_updates['runtime'] = extended['runtime']
            if extended.get('genres'):
                g_str = ', '.join(extended['genres']) if isinstance(extended['genres'], list) else str(extended['genres'])
                movie.genres = g_str
                movie_updates['genres'] = g_str
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
            if i_id and not movie.imdb_id:
                update_data['imdb_id'] = i_id
            if g_list and not movie.genres:
                update_data['genres'] = ', '.join(g_list)
                movie.genres = update_data['genres']
            if movie.group_key:
                Movie.objects.filter(group_key=movie.group_key).update(**update_data)
            else:
                Movie.objects.filter(id=movie.id).update(**update_data)
            movie.imdb_rating = rate
            if i_id and not movie.imdb_id:
                movie.imdb_id = i_id
            if detail:
                detail.imdb_rating = rate
                if i_id and not detail.imdb_id:
                    detail.imdb_id = i_id
                if g_list and not detail.genres:
                    detail.genres = g_list
                detail.save()

    # Fetch all copies sharing the same group_key
    all_copies = []
    if movie.group_key:
        copies_qs = Movie.objects.filter(group_key=movie.group_key).only(
            'id', 'title', 'clean_title', 'file_url', 'source', 'resolution', 'quality', 'language'
        ).order_by('source', '-resolution')
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
        invalidate_catalog_cache()
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


def get_configured_sources():
    """Return all active MediaSource objects, seeding defaults if table is empty."""
    sources = list(MediaSource.objects.filter(is_active=True))
    if not sources and not MediaSource.objects.exists():
        defaults = [
            ('Dhaka-Flix 14', 'http://172.16.50.14/FTP-14/Movies/'),
            ('Dhaka-Flix 7', 'http://172.16.50.7/FTP-7/Movies/'),
            ('E-Box', 'http://10.16.100.244/'),
        ]
        for name, url in defaults:
            MediaSource.objects.create(name=name, url=url, source_type='h5ai')
        sources = list(MediaSource.objects.filter(is_active=True))
    return sources


@manage_auth_required
def manage_dashboard(request):
    """Render the management & maintenance dashboard."""
    stats = _get_catalog_stats(include_manage=True)
    total_files = stats['total_files']
    unique_movies = stats['unique_movies']

    missing_posters = stats['missing_posters_all']
    with_posters = total_files - missing_posters
    poster_pct = round((with_posters / total_files * 100), 1) if total_files else 0

    missing_imdb = stats['missing_imdb_all']
    with_imdb = total_files - missing_imdb
    imdb_pct = round((with_imdb / total_files * 100), 1) if total_files else 0

    missing_genres = stats['missing_genres_all']
    with_genres = total_files - missing_genres
    genres_pct = round((with_genres / total_files * 100), 1) if total_files else 0

    missing_clean = stats['missing_clean_all']

    sources_breakdown = list(Movie.objects.values('source').annotate(count=Count('id')).order_by('-count'))
    resolutions_breakdown = list(Movie.objects.values('resolution').annotate(count=Count('id')).order_by('-count'))
    status_breakdown = list(Movie.objects.values('metadata_status').annotate(count=Count('id')).order_by('-count'))

    from django.db import connection
    recent_crawls = list(CrawledDirectory.objects.order_by('-last_crawled')[:8])
    failed_count = FailedParse.objects.count()
    configured_sources = get_configured_sources()

    context = {
        'db_vendor': connection.vendor,
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
        'configured_sources': configured_sources,
        'task_status': task_manager.get_status(),
    }
    return render(request, 'movies/manage.html', context)


@manage_auth_required
def manage_stats_api(request):
    """API returning live metrics and progress for the management dashboard."""
    stats = _get_catalog_stats(include_manage=True)
    total_files = stats['total_files']
    unique_movies = stats['unique_movies']
    missing_posters = stats['missing_posters_all']
    missing_imdb = stats['missing_imdb_all']
    missing_genres = stats['missing_genres_all']
    missing_clean = stats['missing_clean_all']

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

    paginator = WindowCountPaginator(qs, 20)
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
            invalidate_catalog_cache()
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

        invalidate_catalog_cache()
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
                'runtime': movie.runtime,
            }
        })

    elif action == 'delete':
        title = movie.display_title
        if movie.group_key:
            deleted_count, _ = Movie.objects.filter(group_key=movie.group_key).delete()
        else:
            deleted_count, _ = movie.delete()
        invalidate_catalog_cache()
        return JsonResponse({'success': True, 'message': f"Deleted movie: {title} ({deleted_count} record(s) removed)"})

    return JsonResponse({'success': False, 'message': f"Unknown action: {action}"})


@csrf_exempt
@manage_auth_required
def manage_batch_action_api(request):
    """
    Execute batch operations across multiple selected movies:
    - refetch: enrich metadata for all selected movies
    - delete: delete all selected movies and their server copies
    - set_genre: set genre across all selected movies
    - set_year: set release year across all selected movies
    """
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST.dict()

    action = data.get('action')
    movie_ids = data.get('movie_ids', [])
    if not movie_ids or not isinstance(movie_ids, list):
        return JsonResponse({'success': False, 'message': 'No movies selected'}, status=400)

    # Sanitize IDs
    valid_ids = []
    for mid in movie_ids:
        try:
            valid_ids.append(int(mid))
        except (ValueError, TypeError):
            pass

    if not valid_ids:
        return JsonResponse({'success': False, 'message': 'Invalid movie IDs'}, status=400)

    from movies.models import Movie
    movies = list(Movie.objects.filter(id__in=valid_ids))
    if not movies:
        return JsonResponse({'success': False, 'message': 'No matching movies found'}, status=404)

    if action == 'delete':
        deleted_count = 0
        group_keys = [m.group_key for m in movies if m.group_key]
        standalone_ids = [m.id for m in movies if not m.group_key]
        if group_keys:
            cnt, _ = Movie.objects.filter(group_key__in=group_keys).delete()
            deleted_count += cnt
        if standalone_ids:
            cnt, _ = Movie.objects.filter(id__in=standalone_ids).delete()
            deleted_count += cnt
        invalidate_catalog_cache()
        return JsonResponse({
            'success': True,
            'message': f"Deleted {deleted_count} record(s) across {len(movies)} selected movies.",
            'count': deleted_count,
        })

    elif action == 'refetch':
        from movies.services import enrich_movie_metadata
        success_count = 0
        failed_count = 0
        for m in movies:
            try:
                ok, _ = enrich_movie_metadata(m, include_details=True)
                if ok:
                    success_count += 1
                else:
                    failed_count += 1
            except Exception:
                failed_count += 1
        if success_count > 0:
            invalidate_catalog_cache()
        return JsonResponse({
            'success': True,
            'message': f"Batch enriched: {success_count} succeeded, {failed_count} failed.",
            'success_count': success_count,
            'failed_count': failed_count,
        })

    elif action == 'set_genre':
        genre_val = data.get('value', '').strip()
        if not genre_val:
            return JsonResponse({'success': False, 'message': 'Genre value is required'}, status=400)
        group_keys = [m.group_key for m in movies if m.group_key]
        ids = [m.id for m in movies]
        Movie.objects.filter(id__in=ids).update(genres=genre_val)
        if group_keys:
            Movie.objects.filter(group_key__in=group_keys).update(genres=genre_val)
        invalidate_catalog_cache()
        return JsonResponse({
            'success': True,
            'message': f"Updated genre to '{genre_val}' for {len(movies)} movies.",
            'count': len(movies),
        })

    elif action == 'set_year':
        year_val = data.get('value')
        try:
            year_int = int(year_val)
        except (ValueError, TypeError):
            return JsonResponse({'success': False, 'message': 'Valid year number is required'}, status=400)
        group_keys = [m.group_key for m in movies if m.group_key]
        ids = [m.id for m in movies]
        Movie.objects.filter(id__in=ids).update(year=year_int, year_str=str(year_int))
        if group_keys:
            Movie.objects.filter(group_key__in=group_keys).update(year=year_int, year_str=str(year_int))
        invalidate_catalog_cache()
        return JsonResponse({
            'success': True,
            'message': f"Updated year to {year_int} for {len(movies)} movies.",
            'count': len(movies),
        })

    return JsonResponse({'success': False, 'message': f"Unknown batch action: {action}"}, status=400)


@manage_auth_required
def manage_database_export(request):
    """Export database backup: portable gzipped JSON catalog or checkpointed SQLite file."""
    import os
    import io
    import gzip
    from django.conf import settings
    from django.core.management import call_command
    from django.db import connection
    from django.http import FileResponse, Http404

    fmt = request.GET.get('format', '').strip().lower()
    is_sqlite = (connection.vendor == 'sqlite')

    if fmt == 'json' or not is_sqlite:
        buf = io.StringIO()
        call_command('dumpdata', 'movies', stdout=buf)
        gz_buf = io.BytesIO()
        with gzip.GzipFile(fileobj=gz_buf, mode='wb') as gz:
            gz.write(buf.getvalue().encode('utf-8'))
        gz_buf.seek(0)
        response = HttpResponse(gz_buf.read(), content_type='application/gzip')
        response['Content-Disposition'] = 'attachment; filename="cineverse_catalog_backup.json.gz"'
        return response

    db_path = str(settings.DATABASES['default']['NAME'])
    if not os.path.exists(db_path):
        raise Http404("Database file not found.")

    # Flush SQLite WAL journal into the main db.sqlite3 file before downloading
    try:
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception:
        pass

    response = FileResponse(open(db_path, 'rb'), content_type='application/x-sqlite3')
    response['Content-Disposition'] = 'attachment; filename="cineverse_db_backup.sqlite3"'
    return response


@csrf_exempt
@manage_auth_required
def manage_database_import(request):
    """Restore catalog from .json/.json.gz (PostgreSQL & SQLite) or .sqlite3 (SQLite only)."""
    import os
    import shutil
    import tempfile
    from django.conf import settings
    from django.core.management import call_command
    from django.core.management.color import no_style
    from django.db import connection, connections, transaction

    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    if 'db_file' not in request.FILES:
        return JsonResponse({'success': False, 'message': 'No database file uploaded'}, status=400)

    uploaded_file = request.FILES['db_file']
    fname = uploaded_file.name.lower()

    # Path A: Portable Catalog JSON / Gzipped JSON import (Works on PostgreSQL & SQLite without breaking Auth/Admin)
    if fname.endswith(('.json', '.json.gz')):
        suffix = '.json.gz' if fname.endswith('.json.gz') else '.json'
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                for chunk in uploaded_file.chunks():
                    tmp.write(chunk)
                tmp_path = tmp.name

            with transaction.atomic():
                FailedParse.objects.all().delete()
                CrawledDirectory.objects.all().delete()
                MovieDetail.objects.all().delete()
                Movie.objects.all().delete()
                MediaSource.objects.all().delete()
                call_command('loaddata', tmp_path)

            # Reset PostgreSQL sequences after explicit PK fixture load
            if connection.vendor == 'postgresql':
                sequence_sql = connection.ops.sequence_reset_sql(
                    no_style(), [Movie, MovieDetail, CrawledDirectory, FailedParse, MediaSource]
                )
                with connection.cursor() as cursor:
                    for sql in sequence_sql:
                        cursor.execute(sql)

            invalidate_catalog_cache()
            return JsonResponse({
                'success': True,
                'message': f'Catalog restored successfully! ({Movie.objects.count()} movies loaded). Admin access preserved.'
            })
        except Exception as e:
            return JsonResponse({'success': False, 'message': f"JSON catalog import failed: {str(e)}"}, status=500)
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    # Path B: SQLite Binary File Restore (SQLite environments only)
    if not fname.endswith(('.sqlite3', '.db', '.sqlite')):
        return JsonResponse({'success': False, 'message': 'Invalid file format. Use .json.gz, .json, or .sqlite3.'}, status=400)

    if connection.vendor != 'sqlite':
        return JsonResponse({
            'success': False,
            'message': 'This server uses Online PostgreSQL. Binary .sqlite3 files cannot overwrite PostgreSQL — please upload a .json.gz Catalog Export or run "python manage.py push_local_to_online" from your PC.'
        }, status=400)

    target_path = str(settings.DATABASES['default']['NAME'])
    backup_path = f"{target_path}.bak"

    try:
        header = uploaded_file.read(16)
        uploaded_file.seek(0)
        if not header.startswith(b'SQLite format 3\x00'):
            return JsonResponse({'success': False, 'message': 'Invalid SQLite database file header.'}, status=400)

        # Close active DB connections and remove WAL/SHM sidecar files before replacing db.sqlite3
        connections.close_all()
        if os.path.exists(target_path):
            shutil.copy2(target_path, backup_path)
        for sidecar in (f"{target_path}-wal", f"{target_path}-shm"):
            if os.path.exists(sidecar):
                try:
                    os.remove(sidecar)
                except Exception:
                    pass

        with open(target_path, 'wb+') as destination:
            for chunk in uploaded_file.chunks():
                destination.write(chunk)

        # Re-run migrations and ensure admin account credentials remain active
        call_command('migrate', '--noinput')
        call_command('ensure_admin')
        invalidate_catalog_cache()
        return JsonResponse({'success': True, 'message': 'SQLite database restored and admin verified!'})
    except Exception as e:
        connections.close_all()
        if os.path.exists(backup_path):
            shutil.copy2(backup_path, target_path)
        return JsonResponse({'success': False, 'message': f"Import failed: {str(e)}"}, status=500)


@manage_auth_required
def manage_sources_api(request):
    """API endpoint to list all configured media sources."""
    sources = get_configured_sources()
    data = []
    for s in sources:
        count = Movie.objects.filter(source__iexact=s.name).count()
        data.append({
            'id': s.id,
            'name': s.name,
            'url': s.url,
            'source_type': s.source_type,
            'last_crawled': s.last_crawled.strftime('%Y-%m-%d %H:%M') if s.last_crawled else None,
            'movie_count': count,
        })
    return JsonResponse({'success': True, 'sources': data})


@csrf_exempt
@manage_auth_required
def manage_source_save_api(request):
    """API endpoint to create or update a media source."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST.dict()

    name = data.get('name', '').strip()
    url = data.get('url', '').strip()
    source_type = data.get('source_type', 'h5ai').strip()

    if not name:
        return JsonResponse({'success': False, 'message': 'Source name is required.'}, status=400)
    if not url:
        return JsonResponse({'success': False, 'message': 'Source URL is required.'}, status=400)

    norm_url = normalize_crawl_url(url)

    source, created = MediaSource.objects.update_or_create(
        name=name,
        defaults={
            'url': norm_url,
            'source_type': source_type,
            'is_active': True,
        }
    )
    invalidate_catalog_cache()
    msg = f"Source '{name}' added successfully." if created else f"Source '{name}' updated successfully."
    return JsonResponse({
        'success': True,
        'message': msg,
        'source': {
            'id': source.id,
            'name': source.name,
            'url': source.url,
            'source_type': source.source_type,
        }
    })


@csrf_exempt
@manage_auth_required
def manage_source_delete_api(request):
    """API endpoint to delete a configured media source."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST.dict()

    source_id = data.get('id')
    name = data.get('name')

    deleted = 0
    if source_id:
        deleted, _ = MediaSource.objects.filter(id=source_id).delete()
    elif name:
        deleted, _ = MediaSource.objects.filter(name=name).delete()

    if deleted:
        invalidate_catalog_cache()
        return JsonResponse({'success': True, 'message': 'Source deleted successfully.'})
    return JsonResponse({'success': False, 'message': 'Source not found.'}, status=404)


@csrf_exempt
@manage_auth_required
def manage_source_test_api(request):
    """API endpoint to test reachability and inspect directory contents of a URL."""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'message': 'Method not allowed'}, status=405)

    try:
        data = json.loads(request.body.decode('utf-8'))
    except Exception:
        data = request.POST.dict()

    url = data.get('url', '').strip()
    if not url:
        return JsonResponse({'success': False, 'message': 'URL is required to test connection.'}, status=400)

    result = test_source_connection(url)
    return JsonResponse(result)


def health_check(request):
    """Lightweight readiness probe verifying app process and database connectivity."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
            cursor.fetchone()
        return JsonResponse({'status': 'ok', 'database': connection.vendor})
    except Exception as exc:
        return JsonResponse({'status': 'error', 'detail': str(exc)}, status=503)

