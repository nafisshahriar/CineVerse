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
                if tmdb_id:
                    detail.tmdb_id = tmdb_id
                detail.imdb_id = canonical_imdb_id or ''
                detail.imdb_rating = imdb_rating
                detail.overview = ext.get('overview', '')
                detail.genres = ext.get('genres', [])
                detail.runtime = ext.get('runtime')
                detail.backdrop_url = ext.get('backdrop_url') or backdrop_url
                detail.tagline = ext.get('tagline', '')
                detail.cast = ext.get('cast', [])
                detail.director = ext.get('director', {})
                detail.original_language = ext.get('original_language', '')
                if ext.get('trailer_key'):
                    detail.trailer_key = ext.get('trailer_key')
                    detail.trailer_url = ext.get('trailer_url')
                if official_year and ext.get('release_date'):
                    try:
                        from datetime import datetime
                        detail.release_date = datetime.strptime(str(ext['release_date'])[:10], '%Y-%m-%d').date()
                    except Exception:
                        pass
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
