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
        params = params or {}
        params['api_key'] = self.api_key
        try:
            resp = self.session.get(
                f"{TMDB_BASE_URL}{endpoint}",
                params=params,
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

    def get_movie_details(self, tmdb_id: int) -> Optional[Dict]:
        """Get full movie details by TMDB ID"""
        return self._get(f'/movie/{tmdb_id}')

    def get_extended_details(self, title: str, year: str = '') -> Optional[Dict]:
        """Search and fetch extended movie details in one call with multi-pass search fallback."""
        search_result = self.search_movie_multipass(title, year)
        if not search_result:
            return None

        tmdb_id = search_result.get('id')
        if not tmdb_id:
            return None

        details = self.get_movie_details(tmdb_id)
        if not details:
            return {
                'tmdb_id': tmdb_id,
                'poster_url': self.poster_url(search_result.get('poster_path')),
                'backdrop_url': self.backdrop_url(search_result.get('backdrop_path')),
                'overview': search_result.get('overview', ''),
                'popularity': search_result.get('popularity'),
                'vote_count': search_result.get('vote_count'),
                'vote_average': search_result.get('vote_average'),
                'original_language': search_result.get('original_language', ''),
            }

        return {
            'tmdb_id': tmdb_id,
            'poster_url': self.poster_url(details.get('poster_path')),
            'backdrop_url': self.backdrop_url(details.get('backdrop_path')),
            'overview': details.get('overview', ''),
            'genres': [g['name'] for g in details.get('genres', [])],
            'runtime': details.get('runtime'),
            'release_date': details.get('release_date'),
            'tagline': details.get('tagline', ''),
            'imdb_id': details.get('imdb_id', ''),
            'original_language': details.get('original_language', ''),
            'budget': details.get('budget'),
            'revenue': details.get('revenue'),
            'production_companies': [c['name'] for c in details.get('production_companies', [])],
            'popularity': details.get('popularity'),
            'vote_count': details.get('vote_count'),
            'vote_average': details.get('vote_average'),
            **self._extract_credits(tmdb_id),
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
    - Finds movie on TMDB using multi-pass matching
    - Updates poster_url, popularity, vote_average, vote_count, genres, metadata_status
    - Resolves authentic IMDb rating and IMDb ID
    - Populates MovieDetail (cast, director, overview, backdrop, runtime)
    - Synchronizes all server copies sharing the same group_key atomically
    """
    from django.db import transaction
    from movies.models import Movie, MovieDetail
    from movies.utils import fetch_imdb_rating_and_id, TMDB_GENRES

    search_title = movie.clean_title or movie.title
    year_str = str(movie.year) if movie.year else ''

    res = tmdb_service.search_movie_multipass(search_title, year_str)
    ext = None
    if res and res.get('id'):
        ext = tmdb_service.get_extended_details(search_title, year_str)

    poster_url = None
    backdrop_url = None
    popularity = None
    vote_avg = None
    vote_cnt = None
    tmdb_id = None
    genre_str = None

    if res:
        tmdb_id = res.get('id')
        poster_path = res.get('poster_path')
        poster_url = tmdb_service.poster_url(poster_path) if poster_path else None
        backdrop_path = res.get('backdrop_path')
        backdrop_url = tmdb_service.backdrop_url(backdrop_path) if backdrop_path else None
        popularity = res.get('popularity')
        vote_avg = res.get('vote_average')
        vote_cnt = res.get('vote_count')

        genre_names = []
        if ext and ext.get('genres'):
            genre_names = ext.get('genres')
        elif res.get('genre_ids'):
            genre_names = [TMDB_GENRES.get(gid) for gid in res['genre_ids'] if gid in TMDB_GENRES]
        genre_str = ', '.join([g for g in genre_names if g]) if genre_names else movie.genres

    # Query authentic IMDb ratings using TMDB verified canonical IMDb ID if available
    canonical_imdb_id = (ext.get('imdb_id') if ext else None) or movie.imdb_id
    lookup_year = movie.year
    if ext and ext.get('release_date') and len(ext['release_date']) >= 4 and ext['release_date'][:4].isdigit():
        lookup_year = int(ext['release_date'][:4])

    imdb_res = fetch_imdb_rating_and_id(search_title, lookup_year, known_imdb_id=canonical_imdb_id)
    imdb_rating = imdb_res[0] if (imdb_res and imdb_res[0]) else movie.imdb_rating
    imdb_id = imdb_res[1] if (imdb_res and len(imdb_res) > 1 and imdb_res[1]) else (canonical_imdb_id or movie.imdb_id)
    if imdb_res and len(imdb_res) > 2 and imdb_res[2] and not genre_str:
        genre_str = ', '.join(imdb_res[2])

    if not res and not imdb_rating and not poster_url:
        movie.metadata_status = 'failed'
        movie.save(update_fields=['metadata_status'])
        return False, {'error': 'No matching title found on TMDB or IMDb'}

    update_fields = {
        'metadata_status': 'ok',
    }
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
    if imdb_rating:
        update_fields['imdb_rating'] = imdb_rating
    if imdb_id:
        update_fields['imdb_id'] = imdb_id

    with transaction.atomic():
        for k, v in update_fields.items():
            setattr(movie, k, v)
        movie.save()

        # Synchronize all server copies sharing the same group key
        if movie.group_key:
            Movie.objects.filter(group_key=movie.group_key).update(**update_fields)

        # Extended details
        if include_details and (ext or res):
            try:
                detail, _ = MovieDetail.objects.get_or_create(movie=movie)
                if tmdb_id:
                    detail.tmdb_id = tmdb_id
                if ext:
                    detail.overview = ext.get('overview', '')
                    detail.genres = ext.get('genres', [])
                    detail.runtime = ext.get('runtime')
                    detail.backdrop_url = ext.get('backdrop_url') or backdrop_url
                    detail.tagline = ext.get('tagline', '')
                    detail.cast = ext.get('cast', [])
                    detail.director = ext.get('director', {})
                elif res:
                    detail.overview = res.get('overview', '')
                    detail.backdrop_url = backdrop_url
                detail.save()
            except Exception:
                pass

    return True, {
        'id': movie.id,
        'title': movie.display_title,
        'poster_url': movie.poster_url,
        'imdb_rating': movie.imdb_rating,
        'imdb_id': movie.imdb_id,
        'genres': movie.genres,
        'popularity': movie.popularity,
        'metadata_status': movie.metadata_status,
    }
