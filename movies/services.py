"""
TMDB Service Layer
==================
Centralized service for TMDB API interactions.
Designed for extensibility - add new methods here for additional TMDB features.

Extension Points:
- get_movie_credits() - Get cast and crew
- get_similar_movies() - Get recommendations
- get_movie_videos() - Get trailers
- get_watch_providers() - Streaming availability
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
    
    def get_movie_details(self, tmdb_id: int) -> Optional[Dict]:
        """Get full movie details by TMDB ID"""
        return self._get(f'/movie/{tmdb_id}')
    
    def get_extended_details(self, title: str, year: str = '') -> Optional[Dict]:
        """Search and fetch extended movie details in one call"""
        search_result = self.search_movie(title, year)
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
        
        # Get top 4 cast members
        cast_list = credits.get('cast', [])[:4]
        result['cast'] = [
            {
                'name': c.get('name', ''),
                'character': c.get('character', ''),
                'profile_path': self.profile_url(c.get('profile_path')),
            }
            for c in cast_list
        ]
        
        # Find director in crew
        for crew in credits.get('crew', []):
            if crew.get('job') == 'Director':
                result['director'] = {
                    'name': crew.get('name', ''),
                    'profile_path': self.profile_url(crew.get('profile_path')),
                }
                break
        
        return result
    
    @staticmethod
    def profile_url(path: str, size: str = 'w185') -> Optional[str]:
        """Build full profile image URL from path"""
        if path:
            return f"{TMDB_IMAGE_BASE}{size}{path}"
        return None
    
    def get_movie_credits(self, tmdb_id: int) -> Optional[Dict]:
        """Get cast and crew for a movie - EXTENSION POINT"""
        return self._get(f'/movie/{tmdb_id}/credits')
    
    def get_movie_videos(self, tmdb_id: int) -> Optional[Dict]:
        """Get trailers and videos - EXTENSION POINT"""
        return self._get(f'/movie/{tmdb_id}/videos')
    
    def get_similar_movies(self, tmdb_id: int) -> Optional[Dict]:
        """Get similar movie recommendations - EXTENSION POINT"""
        return self._get(f'/movie/{tmdb_id}/similar')
    
    def get_watch_providers(self, tmdb_id: int, region: str = 'US') -> Optional[Dict]:
        """Get streaming/watch providers - EXTENSION POINT"""
        data = self._get(f'/movie/{tmdb_id}/watch/providers')
        if data and data.get('results'):
            return data['results'].get(region)
        return None
    
    @staticmethod
    def poster_url(path: str, size: str = 'w500') -> Optional[str]:
        """Build full poster URL from path"""
        if path:
            return f"{TMDB_IMAGE_BASE}{size}{path}"
        return None
    
    @staticmethod
    def backdrop_url(path: str, size: str = 'w1280') -> Optional[str]:
        """Build full backdrop URL from path"""
        if path:
            return f"{TMDB_IMAGE_BASE}{size}{path}"
        return None


# Singleton instance for convenience
tmdb_service = TMDBService()


def fetch_poster_and_details(title: str, year: str = '') -> Optional[Dict]:
    """
    Fetch poster and basic details from TMDB.
    This is a compatibility wrapper for the existing crawler.
    """
    result = tmdb_service.get_extended_details(title, year)
    if result:
        return {
            'poster_url': result.get('poster_url'),
            'popularity': result.get('popularity'),
            'vote_count': result.get('vote_count'),
            'vote_average': result.get('vote_average'),
            'tmdb_id': result.get('tmdb_id'),
        }
    return None
