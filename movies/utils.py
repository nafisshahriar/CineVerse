import re
import os
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime, timedelta, timezone as dt_timezone
from django.utils import timezone

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
OMDB_API_KEY = os.getenv('OMDB_API_KEY')

TITLE_YEAR_RE = re.compile(r'^(?P<title>.+?)\s*\((?P<year>\d{4})\)')
# Extended media extensions to catch more file types
MEDIA_EXTS = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.wmv', '.flv', '.ts', '.m2ts')


def parse_title_year(name: str):
    """Parse title and year from a movie folder/file name"""
    # Remove file extension if present
    for ext in MEDIA_EXTS:
        if name.lower().endswith(ext):
            name = name[:-len(ext)]
            break
    
    m = TITLE_YEAR_RE.search(name)
    if m:
        return m.group('title').strip(), m.group('year')
    m2 = re.search(r'(\d{4})', name)
    year = m2.group(1) if m2 else ''
    title = re.sub(r'\b\d{3,4}p\b|\[.*?\]|\(.*?\)', '', name).strip()
    return title, year


def clean_title_for_search(raw: str):
    t = re.sub(r'\[.*?\]', '', raw)
    t = re.sub(r'\d{3,4}p\b', '', t)
    t = re.sub(r'\(.*?\)', '', t)
    return re.sub(r'\s+', ' ', t).strip()


def list_h5ai_directory(url: str, timeout: int = 10):
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, 'html.parser')
    links = []
    # H5AI often uses a table or list; be permissive
    for row in soup.find_all(['tr', 'li', 'div']):
        # find anchor within the row
        a = row.find('a', href=True)
        if not a:
            continue
        href = a['href']
        text = a.get_text(strip=True)
        if not text or href in ('../',) or text in ('Parent Directory', '..'):
            continue
        raw_line = row.get_text(' ', strip=True)
        links.append((text, urljoin(url, href), raw_line))
    # fallback: anchors at top-level
    if not links:
        for a in soup.find_all('a', href=True):
            href = a['href']
            text = a.get_text(strip=True)
            if not text or href in ('../',) or text in ('Parent Directory', '..'):
                continue
            raw_line = a.find_parent().get_text(' ', strip=True) if a.find_parent() else ''
            links.append((text, urljoin(url, href), raw_line))
    return links


def extract_last_modified_from_text(raw_line: str):
    """Extract date from text and return as timezone-aware datetime"""
    if not raw_line:
        return None
    # common ISO style
    m = re.search(r'(20\d{2}-\d{2}-\d{2}\s*\d{2}:\d{2}:\d{2})', raw_line)
    if m:
        try:
            dt = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
            return dt.replace(tzinfo=dt_timezone.utc)
        except Exception:
            pass
    m = re.search(r'(20\d{2}-\d{2}-\d{2})', raw_line)
    if m:
        try:
            dt = datetime.strptime(m.group(1), '%Y-%m-%d')
            return dt.replace(tzinfo=dt_timezone.utc)
        except Exception:
            pass
    m2 = re.search(r'(\d{1,2}-[A-Za-z]{3}-20\d{2})', raw_line)
    if m2:
        try:
            dt = datetime.strptime(m2.group(1), '%d-%b-%Y')
            return dt.replace(tzinfo=dt_timezone.utc)
        except Exception:
            pass
    m3 = re.search(r'(20\d{2})', raw_line)
    if m3:
        yr = int(m3.group(1))
        return datetime(yr, 1, 1, tzinfo=dt_timezone.utc)
    return None


def crawl_h5ai_recursive(root_url: str, per_request_timeout: int = 8, max_items: int = None):
    """
    Recursively crawl H5AI directory structure.
    
    Args:
        root_url: Starting URL to crawl
        per_request_timeout: Timeout for each HTTP request
        max_items: Maximum items to yield (None = unlimited)
    """
    seen = set()
    queue = [root_url]
    yielded = 0
    while queue and (max_items is None or yielded < max_items):
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)
        try:
            items = list_h5ai_directory(cur, timeout=per_request_timeout)
        except requests.exceptions.Timeout:
            # skip directories that timeout
            continue
        except Exception:
            continue
        for text, href, raw in items:
            is_dir = href.endswith('/')
            yielded += 1
            yield {'name': text, 'url': href, 'is_dir': is_dir, 'raw': raw}
            if is_dir:
                queue.append(href)


def find_media_in_directory(url: str, timeout: int = 6):
    try:
        items = list_h5ai_directory(url, timeout=timeout)
    except Exception:
        return []
    media = []
    for text, href, raw in items:
        low = text.lower()
        if any(low.endswith(ext) for ext in MEDIA_EXTS):
            media.append((text, href, raw))
    return media


def fetch_tmdb_details(title: str, year: str = ''):
    if not TMDB_API_KEY:
        return None
    q = clean_title_for_search(title)
    params = {'api_key': TMDB_API_KEY, 'query': q}
    if year:
        params['year'] = year
    r = requests.get('https://api.themoviedb.org/3/search/movie', params=params, timeout=8)
    r.raise_for_status()
    data = r.json()
    results = data.get('results') or []
    if not results:
        return None
    movie = results[0]
    out = {
        'poster_url': ('https://image.tmdb.org/t/p/w500' + movie['poster_path']) if movie.get('poster_path') else None,
        'popularity': movie.get('popularity'),
        'vote_count': movie.get('vote_count'),
        'vote_average': movie.get('vote_average'),
    }
    return out


def fetch_poster_omdb(title: str, year: str = ''):
    if not OMDB_API_KEY:
        return None
    params = {'apikey': OMDB_API_KEY, 't': clean_title_for_search(title)}
    if year:
        params['y'] = year
    r = requests.get('http://www.omdbapi.com/', params=params, timeout=6)
    r.raise_for_status()
    data = r.json()
    poster = data.get('Poster')
    if poster and poster != 'N/A':
        return poster
    return None


def fetch_poster_and_details(title: str, year: str = ''):
    try:
        details = fetch_tmdb_details(title, year)
        if details:
            return details
    except Exception:
        pass
    try:
        poster = fetch_poster_omdb(title, year)
        if poster:
            return {'poster_url': poster}
    except Exception:
        pass
    return None
