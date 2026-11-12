"""
TMDB Service Layer
==================
Centralized service for TMDB API interactions.
Designed for extensibility - add new methods here for additional TMDB features.
"""

import os
import requests
from typing import Optional, Dict, Any, List
from datetime import datetime

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
TMDB_BASE_URL = 'https://api.themoviedb.org/3'
TMDB_IMAGE_BASE = 'https://image.tmdb.org/t/p/'


class TMDBService:
    """Service class for TMDB API interactions"""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or TMDB_API_KEY
        self.session = requests.Session()
        self.timeout = 10

    def _get(self, endpoint: str, params: dict = None) -> Optional[Dict]:
        """Base GET request to TMDB API"""
        if not self.api_key:
            return None
        req_params = dict(params) if params else {}
        req_params['api_key'] = self.api_key
        try:
            resp = self.session.get(
                f"{TMDB_BASE_URL}{endpoint}",
                params=req_params,
                timeout=self.timeout
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.Timeout:
            return None
        except requests.exceptions.RequestException:
            return None
        except Exception:
            return None

    def search_movie(self, title: str, year: str = '') -> Optional[Dict]:
        """Search for a movie by title and optional year"""
        params = {'query': title}
        if year:
            params['year'] = year
        data = self._get('/search/movie', params)
        if data and data.get('results'):
            return data['results'][0]
        return None

    def search_movie_candidates(self, title: str, year: str = '') -> List[Dict]:
        """Search for movies returning all result candidates."""
        params = {'query': title}
        if year:
            params['year'] = year
        data = self._get('/search/movie', params)
        return data.get('results', []) if data else []

    def search_movie_multipass(self, title: str, year: str = '') -> Optional[Dict]:
        """
        Robust multi-pass TMDB search:
        Pass 1: Cleaned title + exact release year.
        Pass 2: Adjacent years (year - 1, year + 1) for festival vs theatrical release discrepancies.
        Pass 3: Track / scene numbers stripped (e.g. '02Nin...' -> 'Nin...') with exact and adjacent years.
        Pass 4: Fallback to all candidates for title, picking the candidate closest in release year!
        Pass 5: Raw title search.
        """
        import re
        if not title:
            return None

        def clean_query(t: str) -> str:
            t = re.sub(r'[°•\*\+~`!@#\$%\^&={}\[\]|\\<>\?]+', ' ', t)
            t = re.sub(r'[\._\-]+', ' ', t)
            t = re.sub(r'\s+', ' ', t).strip()
            return t

        clean_t = clean_query(title)
        year_str = str(year).strip() if year else ''
        target_year = int(year_str) if (year_str and year_str.isdigit()) else None

        # Pass 1: Cleaned title + Exact Year
        if clean_t and year_str:
            res = self.search_movie(clean_t, year_str)
            if res:
                return res

        # Pass 2: Cleaned title + Adjacent Years (±1 year for festival/theatrical offset)
        if clean_t and target_year:
            for adj_y in (target_year - 1, target_year + 1):
                res = self.search_movie(clean_t, str(adj_y))
                if res:
                    return res

        # Pass 3: Strip leading track numbers/prefixes with exact and adjacent years
        alt_title = re.sub(r'^\d{1,3}\s*(?:[\.\-_\s]*|(?=[A-Za-z]))', '', clean_t).strip()
        if alt_title and alt_title != clean_t:
            if year_str:
                res = self.search_movie(alt_title, year_str)
                if res:
                    return res
                if target_year:
                    for adj_y in (target_year - 1, target_year + 1):
                        res = self.search_movie(alt_title, str(adj_y))
                        if res:
                            return res
            else:
                res = self.search_movie(alt_title, '')
                if res:
                    return res

        # Pass 4: Search candidates without year, picking candidate closest in release year
        for query_candidate in (clean_t, alt_title):
            if not query_candidate:
                continue
            candidates = self.search_movie_candidates(query_candidate, '')
            if candidates:
                if target_year:
                    def year_diff(c):
                        rd = c.get('release_date', '')
                        if rd and len(rd) >= 4 and rd[:4].isdigit():
                            return abs(int(rd[:4]) - target_year)
                        return 999
                    best_candidate = min(candidates, key=year_diff)
                    if year_diff(best_candidate) <= 3:
                        return best_candidate
                else:
                    return candidates[0]

        # Pass 5: Raw title search
        raw_cleaned = clean_query(re.sub(r'\.\w{2,4}$', '', title))
        if raw_cleaned and raw_cleaned not in (clean_t, alt_title):
            candidates = self.search_movie_candidates(raw_cleaned, '')
            if candidates:
                if target_year:
                    def year_diff(c):
                        rd = c.get('release_date', '')
                        if rd and len(rd) >= 4 and rd[:4].isdigit():
                            return abs(int(rd[:4]) - target_year)
                        return 999
                    best_candidate = min(candidates, key=year_diff)
                    if year_diff(best_candidate) <= 3:
                        return best_candidate
                else:
                    return candidates[0]

        return None

    def find_by_imdb_id(self, imdb_id: str) -> Optional[Dict]:
        """
        Lookup movie or TV show on TMDB directly by IMDb ID (/find/{imdb_id}?external_source=imdb_id).
        Returns the top matching movie or TV candidate dictionary.
        """
        from movies.utils import normalize_imdb_id
        clean_id = normalize_imdb_id(imdb_id)
        if not clean_id:
            return None
        data = self._get(f'/find/{clean_id}', {'external_source': 'imdb_id'})
        if data:
            movies = data.get('movie_results', [])
            if movies:
                return movies[0]
            tv = data.get('tv_results', [])
            if tv:
                return tv[0]
        return None

    def get_movie_details(self, tmdb_id: int) -> Optional[Dict]:
        """Get full movie details by TMDB ID"""
        return self._get(f'/movie/{tmdb_id}')

    def get_extended_details(self, title: str, year: str = '', imdb_id: str = '') -> Optional[Dict]:
        """
        Search and fetch extended movie details with priority resolution:
        1. If imdb_id is provided, resolve directly via TMDB /find/{imdb_id}.
        2. If TMDB has no match or no API key, fallback to Cinemeta via IMDb ID.
        3. If no imdb_id or not found, fall back to robust multi-pass search.
        """
        from movies.utils import normalize_imdb_id, fetch_cinemeta_details

        clean_imdb = normalize_imdb_id(imdb_id)
        search_result = None

        if clean_imdb:
            search_result = self.find_by_imdb_id(clean_imdb)

        if not search_result and title:
            search_result = self.search_movie_multipass(title, year)

        # Fallback to Cinemeta if TMDB had no match (or no API key) and IMDb ID is available
        if not search_result:
            if clean_imdb:
                cm = fetch_cinemeta_details(clean_imdb)
                if cm:
                    # If Cinemeta returned a moviedb_id and TMDB API key is active, try TMDB details
                    if cm.get('moviedb_id') and self.api_key:
                        details = self.get_movie_details(cm['moviedb_id'])
                        if details:
                            rel_date = details.get('release_date') or cm['release_date']
                            return {
                                'tmdb_id': cm['moviedb_id'],
                                'title': details.get('title') or cm['title'],
                                'release_date': rel_date,
                                'poster_url': self.poster_url(details.get('poster_path')) or cm['poster_url'],
                                'backdrop_url': self.backdrop_url(details.get('backdrop_path')) or cm['backdrop_url'],
                                'overview': details.get('overview') or cm['overview'],
                                'genres': [g['name'] for g in details.get('genres', [])] or cm['genres'],
                                'runtime': details.get('runtime') or cm['runtime'],
                                'tagline': details.get('tagline', ''),
                                'imdb_id': clean_imdb,
                                'imdb_rating': cm['imdb_rating'],
                                'original_language': details.get('original_language', ''),
                                'spoken_languages': [l.get('english_name') for l in details.get('spoken_languages', []) if l.get('english_name')],
                                'budget': details.get('budget'),
                                'revenue': details.get('revenue'),
                                'production_companies': [c['name'] for c in details.get('production_companies', [])],
                                'popularity': details.get('popularity'),
                                'vote_count': details.get('vote_count'),
                                'vote_average': details.get('vote_average'),
                                **(self.get_movie_trailer(cm['moviedb_id']) or {'trailer_key': cm['trailer_key'], 'trailer_url': cm['trailer_url']}),
                                **self._extract_credits(cm['moviedb_id']),
                            }
                    # Return Cinemeta metadata directly
                    return {
                        'tmdb_id': cm.get('moviedb_id'),
                        'title': cm['title'],
                        'release_date': cm['release_date'],
                        'poster_url': cm['poster_url'],
                        'backdrop_url': cm['backdrop_url'],
                        'overview': cm['overview'],
                        'genres': cm['genres'],
                        'runtime': cm['runtime'],
                        'tagline': '',
                        'imdb_id': clean_imdb,
                        'imdb_rating': cm['imdb_rating'],
                        'original_language': '',
                        'spoken_languages': [],
                        'budget': None,
                        'revenue': None,
                        'production_companies': [],
                        'popularity': None,
                        'vote_count': None,
                        'vote_average': cm['imdb_rating'],
                        'trailer_key': cm['trailer_key'],
                        'trailer_url': cm['trailer_url'],
                        'cast': cm['cast'],
                        'director': cm['director'],
                    }
            return None

        tmdb_id = search_result.get('id')
        if not tmdb_id:
            return None

        details = self.get_movie_details(tmdb_id)
        official_title = (details.get('title') if details else None) or search_result.get('title') or search_result.get('name') or title
        rel_date = (details.get('release_date') if details else None) or search_result.get('release_date') or search_result.get('first_air_date')
        res_imdb = clean_imdb or (details.get('imdb_id') if details else None) or ''

        if not details:
            return {
                'tmdb_id': tmdb_id,
                'title': official_title,
                'release_date': rel_date,
                'poster_url': self.poster_url(search_result.get('poster_path')),
                'backdrop_url': self.backdrop_url(search_result.get('backdrop_path')),
                'overview': search_result.get('overview', ''),
                'popularity': search_result.get('popularity'),
                'vote_count': search_result.get('vote_count'),
                'vote_average': search_result.get('vote_average'),
                'original_language': search_result.get('original_language', ''),
                'imdb_id': res_imdb,
            }

        return {
            'tmdb_id': tmdb_id,
            'title': official_title,
            'release_date': rel_date,
            'poster_url': self.poster_url(details.get('poster_path')),
            'backdrop_url': self.backdrop_url(details.get('backdrop_path')),
            'overview': details.get('overview', ''),
            'genres': [g['name'] for g in details.get('genres', [])],
            'runtime': details.get('runtime'),
            'tagline': details.get('tagline', ''),
            'imdb_id': res_imdb or details.get('imdb_id', ''),
            'original_language': details.get('original_language', ''),
            'spoken_languages': [l.get('english_name') for l in details.get('spoken_languages', []) if l.get('english_name')],
            'budget': details.get('budget'),
            'revenue': details.get('revenue'),
            'production_companies': [c['name'] for c in details.get('production_companies', [])],
            'popularity': details.get('popularity'),
            'vote_count': details.get('vote_count'),
            'vote_average': details.get('vote_average'),
            **(self.get_movie_trailer(tmdb_id) or {}),
            **self._extract_credits(tmdb_id),
        }

    def get_movie_trailer(self, tmdb_id: int) -> Optional[Dict]:
        """Fetch official YouTube trailer for a movie."""
        data = self._get(f'/movie/{tmdb_id}/videos')
        if not data or not data.get('results'):
            return None
        videos = data.get('results', [])
        yt_vids = [
            v for v in videos
            if v.get('site') == 'YouTube' and v.get('type') in ('Trailer', 'Teaser') and v.get('key')
        ]
        if not yt_vids:
            return None
        yt_vids.sort(
            key=lambda v: (
                1 if v.get('official') else 0,
                1 if v.get('type') == 'Trailer' else 0,
            ),
            reverse=True
        )
        best = yt_vids[0]
        return {
            'trailer_key': best.get('key'),
            'trailer_url': f"https://www.youtube.com/watch?v={best.get('key')}",
            'trailer_name': best.get('name', 'Official Trailer'),
        }

    def _extract_credits(self, tmdb_id: int) -> Dict:
        """Extract top cast and director from movie credits"""
        credits = self.get_movie_credits(tmdb_id)
        result = {'cast': [], 'director': {}}

        if not credits:
            return result

        cast_list = credits.get('cast', [])[:4]
        result['cast'] = [
            {
                'name': c.get('name', ''),
                'character': c.get('character', ''),
                'profile_path': self.profile_url(c.get('profile_path')),
            }
            for c in cast_list
        ]

        for crew in credits.get('crew', []):
            if crew.get('job') == 'Director':
                result['director'] = {
                    'name': crew.get('name', ''),
                    'profile_path': self.profile_url(crew.get('profile_path')),
                }
                break

        return result

    def get_movie_changes(self, start_date: str = None, end_date: str = None, page: int = 1) -> Optional[Dict]:
        """
        Fetch movie IDs that changed on TMDB in date range (YYYY-MM-DD).
        Powers the sustainable metric refresh strategy.
        """
        params = {'page': page}
        if start_date:
            params['start_date'] = start_date
        if end_date:
            params['end_date'] = end_date
        return self._get('/movie/changes', params)

    @staticmethod
    def profile_url(path: str, size: str = 'w185') -> Optional[str]:
        if path:
            return f"{TMDB_IMAGE_BASE}{size}{path}"
        return None

    def get_movie_credits(self, tmdb_id: int) -> Optional[Dict]:
        return self._get(f'/movie/{tmdb_id}/credits')

    def get_movie_videos(self, tmdb_id: int) -> Optional[Dict]:
        return self._get(f'/movie/{tmdb_id}/videos')

    def get_similar_movies(self, tmdb_id: int) -> Optional[Dict]:
        return self._get(f'/movie/{tmdb_id}/similar')

    def get_watch_providers(self, tmdb_id: int, region: str = 'US') -> Optional[Dict]:
        data = self._get(f'/movie/{tmdb_id}/watch/providers')
        if data and data.get('results'):
            return data['results'].get(region)
        return None

    @staticmethod
    def poster_url(path: str, size: str = 'w500') -> Optional[str]:
        if path:
            return f"{TMDB_IMAGE_BASE}{size}{path}"
        return None

    @staticmethod
    def backdrop_url(path: str, size: str = 'w1280') -> Optional[str]:
        if path:
            return f"{TMDB_IMAGE_BASE}{size}{path}"
        return None


# Singleton instance
tmdb_service = TMDBService()


def fetch_poster_and_details(title: str, year: str = '') -> Optional[Dict]:
    """Compatibility wrapper for crawler."""
    result = tmdb_service.get_extended_details(title, year)
    if result:
        genres = result.get('genres', [])
        return {
            'poster_url': result.get('poster_url'),
            'popularity': result.get('popularity'),
            'vote_count': result.get('vote_count'),
            'vote_average': result.get('vote_average'),
            'tmdb_id': result.get('tmdb_id'),
            'genres': genres,
            'genres_str': ', '.join(genres) if genres else '',
            'original_language': result.get('original_language', ''),
        }
    return None


def enrich_movie_metadata(movie, include_details: bool = True) -> tuple[bool, dict]:
    """
    Unified metadata enricher:
    - Resolves by authentic IMDb ID (movie.imdb_id) as primary key if present
    - Otherwise falls back to TMDB multi-pass title matching
    - Updates clean_title and year to official resolved values
    - Updates poster_url, popularity, vote_average, vote_count, genres, metadata_status, runtime
    - Populates MovieDetail (cast, director, overview, backdrop, runtime, trailer)
    - Synchronizes all server copies sharing the same group_key atomically
    """
    from django.db import transaction
    from movies.models import Movie, MovieDetail
    from movies.utils import fetch_imdb_rating_and_id, TMDB_GENRES, normalize_imdb_id, fetch_cinemeta_details

    known_imdb_id = normalize_imdb_id(movie.imdb_id)
    search_title = movie.clean_title or movie.title
    year_str = str(movie.year) if movie.year else ''

    res = None
    ext = None

    # Step 1: Prioritize exact resolution via IMDb ID if present
    if known_imdb_id:
        ext = tmdb_service.get_extended_details(search_title, year_str, imdb_id=known_imdb_id)
        if ext and ext.get('tmdb_id'):
            res = {'id': ext['tmdb_id'], 'poster_path': None}

    # Step 2: Fall back to title search if no IMDb ID or if IMDb lookup returned nothing
    if not ext:
        res = tmdb_service.search_movie_multipass(search_title, year_str)
        if res and res.get('id'):
            ext = tmdb_service.get_extended_details(search_title, year_str)

    poster_url = None
    backdrop_url = None
    popularity = None
    vote_avg = None
    vote_cnt = None
    tmdb_id = None
    genre_str = None
    official_title = None
    official_year = None

    if ext:
        official_title = ext.get('title')
        if ext.get('release_date') and len(ext['release_date']) >= 4 and ext['release_date'][:4].isdigit():
            official_year = int(ext['release_date'][:4])
        elif ext.get('year'):
            official_year = ext['year']
        poster_url = ext.get('poster_url')
        backdrop_url = ext.get('backdrop_url')
        popularity = ext.get('popularity')
        vote_avg = ext.get('vote_average')
        vote_cnt = ext.get('vote_count')
        tmdb_id = ext.get('tmdb_id')
        if ext.get('genres'):
            genre_str = ', '.join(ext['genres']) if isinstance(ext['genres'], list) else ext['genres']

    elif res:
        tmdb_id = res.get('id')
        poster_path = res.get('poster_path')
        poster_url = tmdb_service.poster_url(poster_path) if poster_path else None
        backdrop_path = res.get('backdrop_path')
        backdrop_url = tmdb_service.backdrop_url(backdrop_path) if backdrop_path else None
        popularity = res.get('popularity')
        vote_avg = res.get('vote_average')
        vote_cnt = res.get('vote_count')
        if res.get('genre_ids'):
            genre_names = [TMDB_GENRES.get(gid) for gid in res['genre_ids'] if gid in TMDB_GENRES]
            genre_str = ', '.join([g for g in genre_names if g])

    # Authentic IMDb Rating & IMDb ID
    # Preserve known_imdb_id: Never overwrite user's set IMDb ID with an ext ID if known_imdb_id is set
    canonical_imdb_id = known_imdb_id or (ext.get('imdb_id') if ext else None) or movie.imdb_id
    lookup_year = official_year or movie.year

    imdb_rating = ext.get('imdb_rating') if ext else None
    if imdb_rating is None:
        imdb_res = fetch_imdb_rating_and_id(official_title or search_title, lookup_year, known_imdb_id=canonical_imdb_id)
        if imdb_res and imdb_res[0] is not None:
            imdb_rating = imdb_res[0]
        elif canonical_imdb_id:
            # Verified IMDb ID has no rating yet on IMDb/Cinemeta (e.g. unrated/upcoming release)
            imdb_rating = None
        else:
            imdb_rating = movie.imdb_rating
        if not canonical_imdb_id and imdb_res and len(imdb_res) > 1 and imdb_res[1]:
            canonical_imdb_id = imdb_res[1]
        if imdb_res and len(imdb_res) > 2 and imdb_res[2] and not genre_str:
            genre_str = ', '.join(imdb_res[2])

    if not res and not ext and imdb_rating is None and not poster_url:
        movie.metadata_status = 'failed'
        movie.save(update_fields=['metadata_status'])
        return False, {'error': 'No matching title found on TMDB or IMDb'}

    update_fields = {
        'metadata_status': 'ok',
    }
    if official_title:
        update_fields['clean_title'] = official_title
    if official_year:
        update_fields['year'] = official_year
        update_fields['year_str'] = str(official_year)
    if poster_url:
        update_fields['poster_url'] = poster_url
    if popularity is not None:
        update_fields['popularity'] = popularity
    if vote_avg is not None:
        update_fields['vote_average'] = vote_avg
    if vote_cnt is not None:
        update_fields['vote_count'] = vote_cnt
    if genre_str:
        update_fields['genres'] = genre_str
    update_fields['imdb_rating'] = imdb_rating
    if canonical_imdb_id:
        update_fields['imdb_id'] = canonical_imdb_id
    if ext and ext.get('runtime'):
        update_fields['runtime'] = ext['runtime']

    with transaction.atomic():
        for k, v in update_fields.items():
            setattr(movie, k, v)
        movie.save()

        # Synchronize all server copies sharing the same group key
        if movie.group_key:
            Movie.objects.filter(group_key=movie.group_key).update(**update_fields)

        # Extended details
        if include_details and ext:
            try:
                detail, _ = MovieDetail.objects.get_or_create(movie=movie)
                for k, v in build_movie_detail_fields(
                    ext,
                    fallback_backdrop=backdrop_url,
                    canonical_imdb_id=canonical_imdb_id,
                    imdb_rating=imdb_rating,
                ).items():
                    setattr(detail, k, v)
                detail.save()
            except Exception:
                pass
        elif include_details and res:
            try:
                detail, _ = MovieDetail.objects.get_or_create(movie=movie)
                if tmdb_id:
                    detail.tmdb_id = tmdb_id
                detail.overview = res.get('overview', '')
                detail.backdrop_url = backdrop_url
                detail.save()
            except Exception:
                pass

    return True, {
        'id': movie.id,
        'title': movie.display_title,
        'year': movie.year,
        'poster_url': movie.poster_url,
        'imdb_rating': movie.imdb_rating,
        'imdb_id': movie.imdb_id,
        'runtime': movie.runtime,
        'genres': movie.genres,
        'popularity': movie.popularity,
        'metadata_status': movie.metadata_status,
    }


def build_movie_detail_fields(
    ext: dict,
    fallback_backdrop: Optional[str] = None,
    canonical_imdb_id: Optional[str] = None,
    imdb_rating: Optional[float] = None,
) -> Dict[str, Any]:
    """Map TMDB/extended metadata dict into MovieDetail model field values."""
    fields: Dict[str, Any] = {
        'overview': ext.get('overview', '') or '',
        'genres': ext.get('genres', []) or [],
        'runtime': ext.get('runtime'),
        'backdrop_url': ext.get('backdrop_url') or fallback_backdrop,
        'tagline': (ext.get('tagline') or '')[:512],
        'cast': ext.get('cast', []) or [],
        'director': ext.get('director', {}) or {},
        'original_language': (ext.get('original_language') or '')[:10],
    }
    if ext.get('tmdb_id'):
        fields['tmdb_id'] = ext['tmdb_id']
    imdb_val = canonical_imdb_id if canonical_imdb_id is not None else ext.get('imdb_id')
    if imdb_val is not None:
        fields['imdb_id'] = (imdb_val or '')[:20]
    rating_val = imdb_rating if imdb_rating is not None else ext.get('imdb_rating')
    if rating_val is not None:
        fields['imdb_rating'] = rating_val
    if ext.get('trailer_key'):
        fields['trailer_key'] = str(ext['trailer_key'])[:64]
        fields['trailer_url'] = (ext.get('trailer_url') or '')[:512]
    if ext.get('release_date'):
        try:
            fields['release_date'] = datetime.strptime(str(ext['release_date'])[:10], '%Y-%m-%d').date()
        except (ValueError, TypeError):
            pass
    return fields


def reconcile_copy_groups(group_keys=None, stop_check=None, log_fn=None) -> Dict[str, int]:
    """Recalculate primary copy flags and propagate group metadata across copies.

    When ``group_keys`` is provided, only inspects movies matching those keys (1-2
    queries after a crawl). When ``None``, scans the full catalog for regrouping.
    """
    import os
    from urllib.parse import unquote
    from django.db import close_old_connections
    from movies.models import Movie
    from movies.utils import parse_movie_info

    full_scan = group_keys is None
    if not full_scan:
        clean_keys = {k for k in group_keys if k}
        if not clean_keys:
            return {'groups': 0, 'updated': 0}
        movies = list(Movie.objects.filter(group_key__in=clean_keys))
    else:
        if log_fn:
            log_fn("Analyzing and regrouping movies across all servers...")
        movies = list(Movie.objects.all())

    total = len(movies)
    if total == 0:
        return {'groups': 0, 'updated': 0}

    if full_scan and log_fn:
        log_fn(f"Total files in catalog: {total}")

    res_priority = {'4k': 4, '1080p': 3, '720p': 2, '480p': 1}
    shared_fields = (
        'clean_title', 'poster_url', 'imdb_id', 'imdb_rating', 'genres',
        'runtime', 'popularity', 'vote_average', 'vote_count',
    )

    groups: Dict[str, List[Any]] = {}
    changed_field_names = set()
    dirty_ids = set()

    for idx, m in enumerate(movies):
        if stop_check and stop_check():
            if log_fn:
                log_fn("[STOPPED] Regrouping cancelled by user.")
            return {'groups': len(groups), 'updated': 0, 'stopped': True}

        if full_scan and log_fn and idx % 1000 == 0:
            pct = round((idx / total) * 100, 1)
            log_fn(f"[{idx}/{total} ({pct}%)] Grouping movies...")

        if full_scan and (not m.clean_title or not m.group_key or not m.resolution or not m.source):
            file_name = os.path.basename(unquote(m.file_url or ''))
            dir_name = os.path.basename(unquote(m.directory_url or '').rstrip('/\\'))
            info = parse_movie_info(dir_name or file_name, file_name)
            if not m.clean_title:
                m.clean_title = (info.get('clean_title') or m.title)[:512]
                changed_field_names.add('clean_title')
                dirty_ids.add(m.id)
            if not m.group_key:
                m.group_key = (info.get('group_key') or '')[:512]
                changed_field_names.add('group_key')
                dirty_ids.add(m.id)
            if not m.resolution and info.get('resolution'):
                m.resolution = info['resolution'][:20]
                changed_field_names.add('resolution')
                dirty_ids.add(m.id)
            if not m.source and info.get('source'):
                m.source = info['source'][:64]
                changed_field_names.add('source')
                dirty_ids.add(m.id)

        key = m.group_key or f"{(m.clean_title or m.title).lower()}:::{m.year or ''}"
        groups.setdefault(key, []).append(m)

    if full_scan and log_fn:
        log_fn(f"Found {len(groups)} unique movie groups across {total} files.")
        log_fn("Assigning primary copies (prioritizing 4K > 1080P > 720P)...")

    movies_to_update = []
    for copies in groups.values():
        # Propagate non-empty shared metadata within the group so promoted copies keep posters/ratings
        shared_best = {}
        for field in shared_fields:
            for c in copies:
                val = getattr(c, field)
                if val not in (None, ''):
                    shared_best[field] = val
                    break

        best_fetched = any(c.fetched for c in copies)

        copies.sort(
            key=lambda c: (
                res_priority.get((c.resolution or '').lower(), 0),
                1 if c.poster_url else 0,
                1 if c.fetched else 0,
                1 if c.source else 0,
                -c.id,
            ),
            reverse=True,
        )

        for i, c in enumerate(copies):
            dirty = c.id in dirty_ids
            should_be_primary = (i == 0)
            if c.is_primary_copy != should_be_primary:
                c.is_primary_copy = should_be_primary
                changed_field_names.add('is_primary_copy')
                dirty = True

            if len(copies) > 1:
                for field, best_val in shared_best.items():
                    if getattr(c, field) in (None, ''):
                        setattr(c, field, best_val)
                        changed_field_names.add(field)
                        dirty = True
                if best_fetched and not c.fetched:
                    c.fetched = True
                    c.metadata_status = 'ok'
                    changed_field_names.update(['fetched', 'metadata_status'])
                    dirty = True

            if dirty:
                movies_to_update.append(c)

    if movies_to_update and changed_field_names:
        if log_fn:
            log_fn(f"Updating {len(movies_to_update)} movie copy records in database...")
        fields_list = sorted(changed_field_names)
        for i in range(0, len(movies_to_update), 500):
            chunk = movies_to_update[i:i + 500]
            close_old_connections()
            Movie.objects.bulk_update(chunk, fields_list)
        if log_fn:
            log_fn(f"[OK] Successfully reconciled primary copy flags for {len(movies_to_update)} records.")
    elif log_fn:
        log_fn("[OK] All copy groups are already optimal and up-to-date!")

    return {'groups': len(groups), 'updated': len(movies_to_update)}
