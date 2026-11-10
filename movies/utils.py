import re
import os
import time
import ipaddress
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse, unquote, urlunparse
from datetime import datetime, timedelta, timezone as dt_timezone
from django.utils import timezone

TMDB_API_KEY = os.getenv('TMDB_API_KEY')
OMDB_API_KEY = os.getenv('OMDB_API_KEY')

MEDIA_EXTS = ('.mp4', '.mkv', '.avi', '.mov', '.webm', '.m4v', '.wmv', '.flv', '.ts', '.m2ts')

IGNORED_DIRS = {
    'subs', 'sub', 'subtitle', 'subtitles', 'sample', 'samples',
    'extras', 'extra', 'trailers', 'trailer', 'featurettes', 'featurette'
}

LANGUAGE_MAP = {
    'korean': 'Korean', 'japanese': 'Japanese', 'hindi': 'Hindi',
    'german': 'German', 'french': 'French', 'spanish': 'Spanish',
    'iranian': 'Persian', 'persian': 'Persian', 'turkish': 'Turkish',
    'danish': 'Danish', 'swedish': 'Swedish', 'russian': 'Russian',
    'italian': 'Italian', 'thai': 'Thai', 'chinese': 'Chinese',
    'tamil': 'Tamil', 'telugu': 'Telugu', 'malayalam': 'Malayalam',
    'bengali': 'Bengali', 'bangla': 'Bengali', 'polish': 'Polish',
    'dutch': 'Dutch', 'portuguese': 'Portuguese', 'indonesian': 'Indonesian',
    'norwegian': 'Norwegian', 'arabic': 'Arabic', 'english': 'English'
}

TMDB_LANG_MAP = {
    'en': 'English', 'ko': 'Korean', 'ja': 'Japanese', 'hi': 'Hindi',
    'es': 'Spanish', 'fr': 'French', 'de': 'German', 'it': 'Italian',
    'ru': 'Russian', 'zh': 'Chinese', 'cn': 'Chinese', 'pt': 'Portuguese',
    'fa': 'Persian', 'tr': 'Turkish', 'da': 'Danish', 'sv': 'Swedish',
    'th': 'Thai', 'nl': 'Dutch', 'pl': 'Polish', 'ar': 'Arabic',
    'bn': 'Bengali', 'ta': 'Tamil', 'te': 'Telugu', 'ml': 'Malayalam',
    'id': 'Indonesian', 'no': 'Norwegian', 'fi': 'Finnish', 'el': 'Greek'
}


class ProgressTracker:
    """
    Progress tracker calculating completed, remaining, percentage,
    items/second speed, and estimated time remaining (ETA).
    """
    def __init__(self, total: int, desc: str = "Processing"):
        self.total = max(1, total)
        self.desc = desc
        self.start_time = time.time()
        self.processed = 0
        self.success = 0
        self.failed = 0

    def tick(self, success: bool = True, title: str = "", extra: str = "") -> str:
        self.processed += 1
        if success:
            self.success += 1
        else:
            self.failed += 1

        now = time.time()
        elapsed = max(0.1, now - self.start_time)
        rate = self.processed / elapsed
        remaining = max(0, self.total - self.processed)
        eta_seconds = int(remaining / rate) if rate > 0 else 0
        eta_str = f"{eta_seconds // 60:02d}m {eta_seconds % 60:02d}s"
        percent = (self.processed / self.total) * 100.0

        status = f"[{self.processed}/{self.total}] ({percent:5.1f}%) | {remaining} left | ETA: {eta_str} ({rate:.1f}/s)"
        if title:
            mark = "[OK]" if success else "[FAIL]"
            status += f" | {mark} {title}"
        if extra:
            status += f" ({extra})"
        return status


def detect_source(url: str) -> str:
    """Detect readable server / source name from URL."""
    if not url:
        return 'Local'
    u = unquote(url).lower()
    netloc = urlparse(url).netloc
    if 'dhaka-flix-14' in u or '172.16.50.14' in netloc:
        return 'Dhaka-Flix 14'
    if 'dhaka-flix-7' in u or '172.16.50.7' in netloc:
        return 'Dhaka-Flix 7'
    if 'ebox.live' in netloc:
        return 'E-Box'
    if netloc:
        return netloc
    return 'Local'


def detect_resolution(text: str) -> str:
    """Extract standard resolution tag (4K, 1080p, 720p, 480p)."""
    if not text:
        return ''
    m = re.search(r'(?:^|[^a-zA-Z0-9])(2160p|4k|1080p|720p|480p)(?:$|[^a-zA-Z0-9])', text, flags=re.I)
    if m:
        val = m.group(1).upper()
        return '4K' if val == '2160P' else val
    return ''


def detect_quality(text: str) -> str:
    """Extract release quality (BluRay, WEB-DL, HDRip, DVDRip, etc.)."""
    if not text:
        return ''
    if re.search(r'\b(bluray|blu-ray|bdrip|brrip)\b', text, flags=re.I):
        return 'BluRay'
    if re.search(r'\b(web-dl|webdl|web-rip|webrip|web)\b', text, flags=re.I):
        return 'WEB-DL'
    if re.search(r'\b(hdrip|hdtv)\b', text, flags=re.I):
        return 'HDRip'
    if re.search(r'\b(dvdrip|dvd)\b', text, flags=re.I):
        return 'DVDRip'
    if re.search(r'\b(cam|telesync|hdcam)\b', text, flags=re.I):
        return 'CAM'
    return ''


def detect_language(url: str = '', filename: str = '', tmdb_code: str = '') -> str:
    """Detect language from TMDB code, directory path, or filename."""
    if tmdb_code and tmdb_code.lower() in TMDB_LANG_MAP:
        return TMDB_LANG_MAP[tmdb_code.lower()]
    combined = (unquote(url) + ' ' + unquote(filename)).lower()
    for key, lang in LANGUAGE_MAP.items():
        if key in combined:
            return lang
    return 'English'


def clean_movie_title(raw_name: str) -> tuple[str, str]:
    """
    Clean raw filename or folder into a pure movie title and 4-digit year.
    Handles scene tags, release groups, track numbers, and special characters.
    """
    name = unquote(raw_name).strip()

    # Strip extension
    for ext in MEDIA_EXTS:
        if name.lower().endswith(ext):
            name = name[:-len(ext)].strip()
            break

    # Strip release group / suffix tags: -mkvC, -MkvCage, [E-BOX], _MYFLIX, etc.
    name = re.sub(r'[-_]\s*([a-zA-Z0-9]+)$', '', name)
    name = re.sub(
        r'\[(?:E-BOX|MYFLIX|MkvCage|mkvC|KA|GetS|LION|Mkvking|YTS|RARBG|Dual Audio|Hindi|Tamil|Telugu).*?\]',
        '', name, flags=re.I
    )

    # Strip track numbering at start (e.g. '01 - ', '01.-', '01. ')
    name = re.sub(r'^\d{1,3}\s*[-–—]\s*', '', name)
    name = re.sub(r'^\d{1,3}\.\s+(?=[a-zA-Z])', '', name)

    # Franchise prefixes before a dash: '01-James Bond - Dr. No' -> 'Dr. No'
    if ' - ' in name:
        parts = name.split(' - ')
        if len(parts) == 2 and any(k in parts[0].lower() for k in ['james bond', 'mcu', 'marvel', 'star wars', 'harry potter', 'dc']):
            name = parts[1].strip()

    # Extract year (prefer 4 digits between 1900 and 2039)
    year = ''
    y_match = re.search(r'[\(\[\s_.-](19\d\d|20[0-3]\d)[\)\]\s_.-]', name)
    if not y_match:
        y_match = re.search(r'\b(19\d\d|20[0-3]\d)\b', name)

    if y_match:
        year = y_match.group(1)
        title_part = name[:y_match.start()].strip()
    else:
        title_part = name

    # Scene dot-separated words: replace dots with spaces (protect decimal numbers like 0.0)
    if '.' in title_part and not re.search(r'\b\d+\.\d+\b', title_part):
        title_part = title_part.replace('.', ' ')
    if '_' in title_part:
        title_part = title_part.replace('_', ' ')

    # Strip quality/codec tags before year
    title_part = re.sub(
        r'\b(2160p|4k|1080p|720p|480p|bluray|x264|x265|hevc|web-dl|webrip|hdrip|korean|hindi)\b.*',
        '', title_part, flags=re.I
    )

    # Clean brackets, preserving titles starting with (500)
    if not title_part.startswith('(500)'):
        cleaned_brackets = re.sub(r'\[.*?\]', '', title_part)
        cleaned_parens = re.sub(r'\((?!500\b).*?\)', '', cleaned_brackets)
        # If removing all bracketed content emptied the title (e.g. '[Rec]'), preserve the text inside
        if not cleaned_parens.strip(' -._:,#/\\'):
            title_part = re.sub(r'[\[\]\(\)]', '', title_part)
        else:
            title_part = cleaned_parens

    clean = re.sub(r'\s+', ' ', title_part).strip(' -._:,#/\\')
    return clean or name, year


def parse_movie_info(dir_name: str, file_name: str, file_url: str = '', tmdb_lang: str = '') -> dict:
    """
    Parse complete structured movie information combining directory and file details.
    """
    # If directory name is generic or missing, prioritize filename
    d_clean = unquote(dir_name).strip(' /\\')
    f_clean = unquote(file_name).strip()

    is_generic_dir = not d_clean or re.match(r'^(\[\s*\d{4}\s*\]|\d{4}|subs?)$', d_clean, flags=re.I)
    primary_name = f_clean if is_generic_dir else d_clean

    title, year = clean_movie_title(primary_name)
    if not year and not is_generic_dir:
        # Fallback to check filename for year if folder didn't have it
        _, f_year = clean_movie_title(f_clean)
        year = f_year

    # If title parsed as empty or pure punctuation, fallback to filename
    if not title or len(title) < 2 or title in ('/', '\\', '-'):
        title, year = clean_movie_title(f_clean)

    combined_text = f"{dir_name} {file_name} {file_url}"
    source = detect_source(file_url)
    resolution = detect_resolution(combined_text)
    quality = detect_quality(combined_text)
    language = detect_language(url=file_url, filename=file_name, tmdb_code=tmdb_lang)

    # Group key for deduplicating identical movies across sources
    norm_title = re.sub(r'[^a-zA-Z0-9]+', '', title).lower()
    group_key = f"{norm_title}:::{year or ''}"

    return {
        'title': title,
        'clean_title': title,
        'year': int(year) if year and year.isdigit() else None,
        'year_str': year or '',
        'source': source,
        'resolution': resolution,
        'quality': quality,
        'language': language,
        'group_key': group_key,
    }


def parse_title_year(name: str):
    """Backwards-compatible wrapper for title and year parsing."""
    title, year = clean_movie_title(name)
    return title, year


def clean_title_for_search(raw: str):
    """Backwards-compatible wrapper for TMDB search query preparation."""
    clean, _ = clean_movie_title(raw)
    return clean


CRAWL_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9',
}

NON_DIR_EXTS = (
    '.srt', '.sub', '.vtt', '.idx', '.nfo', '.txt', '.jpg', '.jpeg', '.png', '.gif',
    '.zip', '.rar', '.7z', '.tar', '.gz', '.torrent', '.iso', '.exe', '.bin', '.pdf',
    '.html', '.php', '.css', '.js', '.json', '.xml'
)


def normalize_crawl_url(url: str) -> str:
    """Normalize user-entered crawl URL, ensuring scheme and valid directory trailing slash."""
    if not url:
        return ''
    u = url.strip()
    if not re.match(r'^https?://', u, re.IGNORECASE):
        u = 'http://' + u
    parsed = urlparse(u)
    path = parsed.path or '/'
    # If path does not end with / and does not look like a file with an extension, append /
    if not path.endswith('/'):
        ext = os.path.splitext(path)[1]
        if not ext:
            path += '/'
            u = urlunparse(parsed._replace(path=path))
    return u


def is_directory_link(href: str, text: str = '') -> bool:
    """Determine whether a hyperlink points to a directory rather than a file."""
    clean_href = href.split('?')[0].split('#')[0]
    if clean_href.endswith('/') or (text and text.endswith('/')):
        return True
    low_href = clean_href.lower()
    if any(low_href.endswith(ext) for ext in MEDIA_EXTS):
        return False
    if any(low_href.endswith(ext) for ext in NON_DIR_EXTS):
        return False
    last_segment = unquote(clean_href.rstrip('/').split('/')[-1])
    # If no dot in the last path segment, it is almost certainly a directory
    if '.' not in last_segment:
        return True
    return False


def list_h5ai_directory(url: str, timeout: int = 10, session: requests.Session = None):
    """
    List contents of an H5AI, Apache, Nginx, or Caddy directory.
    Supports both native H5AI JSON API and HTML directory fallbacks.
    Returns list of (text, full_url, raw_line).
    """
    url = normalize_crawl_url(url)
    req = session if session is not None else requests
    resp = req.get(url, headers=CRAWL_HEADERS, timeout=timeout)
    resp.raise_for_status()

    final_url = resp.url
    if not final_url.endswith('/') and not os.path.splitext(urlparse(final_url).path)[1]:
        final_url += '/'

    links = []

    # 1. Native H5AI JSON API check
    is_h5ai = '_h5ai' in resp.text or 'data-h5ai' in resp.text or 'powered by h5ai' in resp.text.lower()
    if is_h5ai:
        try:
            parsed_path = urlparse(final_url).path or '/'
            api_endpoint = urljoin(final_url, '/_h5ai/public/php/api.php')
            post_data = {'action': 'get', 'items': [{'href': parsed_path, 'what': 1}]}
            api_resp = req.post(api_endpoint, json=post_data, headers=CRAWL_HEADERS, timeout=timeout)
            if api_resp.status_code == 200:
                data = api_resp.json()
                for it in data.get('items', []):
                    item_href = it.get('href', '')
                    if not item_href or item_href == parsed_path:
                        continue
                    item_full = urljoin(final_url, item_href)
                    item_text = unquote(item_href.rstrip('/').split('/')[-1])
                    if item_text in ('..', '../', 'Parent Directory', ''):
                        continue
                    raw_info = f"{item_text} {it.get('time', '')} {it.get('size', '')}"
                    links.append((item_text, item_full, raw_info))
        except Exception:
            pass

    # 2. HTML parsing (Apache, Nginx, Caddy, Lighttpd, H5AI fallback tables)
    if not links:
        soup = BeautifulSoup(resp.text, 'html.parser')
        base_netloc = urlparse(final_url).netloc

        for a in soup.find_all('a', href=True):
            raw_href = a['href'].strip()
            text = a.get_text(' ', strip=True)

            if not raw_href or not text:
                continue
            # Ignore sorting query parameters, fragments, and javascript links
            if raw_href.startswith(('?', '#', 'javascript:', 'mailto:')):
                continue

            low_text = text.lower()
            if low_text in ('parent directory', '..', '../', '.', '[to parent directory]', 'back') or low_text.startswith('parent directory'):
                continue
            if raw_href in ('../', '/', '..', '.'):
                continue
            if raw_href.endswith(('.css', '.js')) or '/icons/' in raw_href or '_h5ai' in raw_href:
                continue

            full_href = urljoin(final_url, raw_href)
            full_parsed = urlparse(full_href)

            if full_parsed.netloc and full_parsed.netloc != base_netloc:
                continue
            if full_href.rstrip('/') == final_url.rstrip('/'):
                continue

            if is_directory_link(full_href, text) and not full_href.endswith('/'):
                full_href += '/'

            raw_line = a.find_parent().get_text(' ', strip=True) if a.find_parent() else text
            links.append((text, full_href, raw_line))

    # De-duplicate preserving discovered order
    seen_urls = set()
    unique_links = []
    for t, u, r in links:
        if u not in seen_urls:
            seen_urls.add(u)
            unique_links.append((t, u, r))

    return unique_links


def extract_last_modified_from_text(raw_line: str):
    """
    Extract last modified datetime from raw line or metadata string.
    Supports standard YYYY-MM-DD, Apache/Nginx AutoIndex date formats,
    and H5AI Unix epoch millisecond/second timestamps.
    """
    if not raw_line:
        return None
    # 1. Full datetime: 2024-05-10 14:30:00
    m = re.search(r'(20\d{2}-\d{2}-\d{2}\s*\d{2}:\d{2}:\d{2})', raw_line)
    if m:
        try:
            dt = datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
            return dt.replace(tzinfo=dt_timezone.utc)
        except Exception:
            pass
    # 2. Date with dashes: 2024-05-10
    m = re.search(r'(20\d{2}-\d{2}-\d{2})', raw_line)
    if m:
        try:
            dt = datetime.strptime(m.group(1), '%Y-%m-%d')
            return dt.replace(tzinfo=dt_timezone.utc)
        except Exception:
            pass
    # 3. Apache/Nginx AutoIndex format: 12-Jan-2024 or 12-Jan-2024 14:20
    m2 = re.search(r'(\d{1,2}-[A-Za-z]{3}-20\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)', raw_line)
    if m2:
        val = m2.group(1).strip()
        for fmt in ('%d-%b-%Y %H:%M:%S', '%d-%b-%Y %H:%M', '%d-%b-%Y'):
            try:
                dt = datetime.strptime(val, fmt)
                return dt.replace(tzinfo=dt_timezone.utc)
            except Exception:
                pass
    # 4. Unix epoch timestamps (seconds: 10 digits, milliseconds: 13 digits) - used by H5AI JSON API
    m_epoch = re.search(r'\b(1[5-7]\d{8}(?:\d{3})?)\b', raw_line)
    if m_epoch:
        try:
            val = int(m_epoch.group(1))
            if val > 100000000000:  # milliseconds
                val = val / 1000.0
            return datetime.fromtimestamp(val, tz=dt_timezone.utc)
        except Exception:
            pass
    return None


def is_private_or_local_host(hostname: str) -> bool:
    """Check if hostname/IP points to a private local or BDIX network."""
    if not hostname:
        return False
    host_clean = hostname.split(':')[0].strip().lower()
    if host_clean in ('localhost', '127.0.0.1', '::1') or host_clean.endswith('.local'):
        return True
    try:
        ip = ipaddress.ip_address(host_clean)
        return ip.is_private or ip.is_loopback or ip.is_reserved or ip.is_link_local
    except ValueError:
        pass
    return False


def crawl_h5ai_recursive(root_url: str, per_request_timeout: int = 8, max_items: int = None, should_skip_dir=None):
    """
    Recursively explore directory trees and media files from root_url.
    Yields dictionary for each discovered media file or directory.
    If should_skip_dir is provided and returns True for a directory, recursing into
    that unchanged directory is skipped, saving network round trips.
    Yields error item if root_url cannot be reached so callers get immediate feedback.
    """
    root_url = normalize_crawl_url(root_url)
    if not root_url:
        yield {'error': True, 'message': 'No URL provided for crawling.'}
        return

    session = requests.Session()
    session.headers.update(CRAWL_HEADERS)

    seen = set()
    queue = [root_url]
    yielded = 0

    while queue and (max_items is None or yielded < max_items):
        cur = queue.pop(0)
        if cur in seen:
            continue
        seen.add(cur)

        try:
            items = list_h5ai_directory(cur, timeout=per_request_timeout, session=session)
        except requests.exceptions.Timeout as e:
            if cur == root_url:
                yield {'error': True, 'message': f"Connection timed out reaching root URL: {cur}"}
            continue
        except requests.exceptions.RequestException as e:
            if cur == root_url:
                yield {'error': True, 'message': f"Failed to connect to root URL {cur}: {e}"}
            continue
        except Exception as e:
            if cur == root_url:
                yield {'error': True, 'message': f"Unexpected error reading root URL {cur}: {e}"}
            continue

        if cur == root_url and not items:
            yield {'warning': True, 'message': f"Connected to {cur}, but found 0 directory items or media files."}

        cur_dir_name = unquote(urlparse(cur).path.rstrip('/').split('/')[-1])

        for text, href, raw in items:
            is_dir = is_directory_link(href, text)
            clean_name = text.strip(' /\\').lower()
            if clean_name in IGNORED_DIRS:
                continue

            # If directory is already crawled and unchanged, skip recursing into it
            if is_dir and should_skip_dir and should_skip_dir(href, raw):
                yielded += 1
                yield {
                    'name': text,
                    'url': href,
                    'is_dir': True,
                    'is_skipped': True,
                    'raw': raw,
                    'dir_name': cur_dir_name,
                    'directory_url': cur,
                }
                continue

            yielded += 1
            yield {
                'name': text,
                'url': href,
                'is_dir': is_dir,
                'is_skipped': False,
                'raw': raw,
                'dir_name': cur_dir_name,
                'directory_url': cur,
            }

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


def test_source_connection(url: str, timeout: int = 8) -> dict:
    """
    Test connectivity to a media server / directory URL.
    Returns structured result with server type, status, and item counts.
    """
    norm_url = normalize_crawl_url(url)
    if not norm_url:
        return {'success': False, 'message': 'Please provide a valid URL.'}

    session = requests.Session()
    session.headers.update(CRAWL_HEADERS)

    try:
        resp = session.get(norm_url, timeout=timeout)
        resp.raise_for_status()
    except requests.exceptions.SSLError:
        try:
            resp = session.get(norm_url, timeout=timeout, verify=False)
            resp.raise_for_status()
        except Exception as e:
            return {'success': False, 'message': f"SSL certificate verification failed: {e}"}
    except requests.exceptions.Timeout:
        hostname = urlparse(norm_url).hostname or ''
        if is_private_or_local_host(hostname):
            return {
                'success': False,
                'is_private_host': True,
                'message': (
                    f"Connection timed out reaching '{hostname}'. This is a private local/BDIX network address. "
                    "Cloud hosts (like Render) cannot route to private LAN or local ISP subnets directly over the internet. "
                    "To use this source: (1) Run CineVerse locally or on a Raspberry Pi on the same network, "
                    "(2) Use CineVerse's Database Backup & Restore to sync local movies to Render, or "
                    "(3) Expose your local FTP using Cloudflare Tunnel, Tailscale, or Ngrok."
                )
            }
        return {'success': False, 'message': f"Connection timed out after {timeout}s. Verify server IP and network access."}
    except requests.exceptions.ConnectionError:
        hostname = urlparse(norm_url).hostname or ''
        if is_private_or_local_host(hostname):
            return {
                'success': False,
                'is_private_host': True,
                'message': (
                    f"Connection refused by '{hostname}'. This is a private local/BDIX network address. "
                    "Cloud servers (like Render) cannot reach local intranet or BDIX IPs directly over the internet. "
                    "Run CineVerse locally or on a Raspberry Pi on that network to scan this source, or transfer metadata using Database Backup & Restore."
                )
            }
        return {'success': False, 'message': "Connection refused. Server may be offline, firewalled, or on an unreachable subnet."}
    except requests.exceptions.HTTPError as e:
        status = resp.status_code if 'resp' in locals() else None
        return {'success': False, 'status_code': status, 'message': f"HTTP Error {status}: {e}"}
    except Exception as e:
        return {'success': False, 'message': f"Connection failed: {e}"}

    final_url = resp.url
    is_direct_media = any(final_url.lower().endswith(ext) for ext in MEDIA_EXTS)
    if is_direct_media:
        return {
            'success': True,
            'server_type': 'Direct Media Stream',
            'status_code': resp.status_code,
            'url': final_url,
            'items_count': 1,
            'media_count': 1,
            'dir_count': 0,
            'sample_items': [unquote(os.path.basename(urlparse(final_url).path))],
            'message': 'Connected directly to playable media file!'
        }

    try:
        items = list_h5ai_directory(final_url, timeout=timeout, session=session)
        media_count = sum(1 for text, href, _ in items if not is_directory_link(href, text) and any(href.lower().endswith(ext) for ext in MEDIA_EXTS))
        dir_count = sum(1 for text, href, _ in items if is_directory_link(href, text))

        server_label = 'Open Directory'
        text_lower = resp.text.lower()
        if '_h5ai' in text_lower or 'powered by h5ai' in text_lower:
            server_label = 'H5AI Directory Server'
        elif 'apache' in resp.headers.get('server', '').lower() or 'index of' in text_lower:
            server_label = 'Apache AutoIndex'
        elif 'nginx' in resp.headers.get('server', '').lower():
            server_label = 'Nginx AutoIndex'
        elif 'caddy' in resp.headers.get('server', '').lower():
            server_label = 'Caddy File Server'

        samples = [t for t, _, _ in items[:6]]

        return {
            'success': True,
            'server_type': server_label,
            'status_code': resp.status_code,
            'url': final_url,
            'items_count': len(items),
            'media_count': media_count,
            'dir_count': dir_count,
            'sample_items': samples,
            'message': f"Successfully reached {server_label}! Detected {dir_count} directories and {media_count} media files."
        }
    except Exception as e:
        return {
            'success': True,
            'server_type': 'HTTP Server (Listing unparsed)',
            'status_code': resp.status_code,
            'url': final_url,
            'items_count': 0,
            'media_count': 0,
            'dir_count': 0,
            'sample_items': [],
            'message': f"Server reachable (HTTP {resp.status_code}), but directory listing parser returned: {e}"
        }



TMDB_GENRES = {
    28: 'Action',
    12: 'Adventure',
    16: 'Animation',
    35: 'Comedy',
    80: 'Crime',
    99: 'Documentary',
    18: 'Drama',
    10751: 'Family',
    14: 'Fantasy',
    36: 'History',
    27: 'Horror',
    10402: 'Music',
    9648: 'Mystery',
    10749: 'Romance',
    878: 'Science Fiction',
    10770: 'TV Movie',
    53: 'Thriller',
    10752: 'War',
    37: 'Western',
}


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
    genre_names = [TMDB_GENRES[gid] for gid in movie.get('genre_ids', []) if gid in TMDB_GENRES]
    out = {
        'poster_url': ('https://image.tmdb.org/t/p/w500' + movie['poster_path']) if movie.get('poster_path') else None,
        'popularity': movie.get('popularity'),
        'vote_count': movie.get('vote_count'),
        'vote_average': movie.get('vote_average'),
        'genres': genre_names,
        'genres_str': ', '.join(genre_names),
        'tmdb_id': movie.get('id'),
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


def normalize_imdb_id(raw_id) -> str | None:
    """
    Extract canonical IMDb ID (tt followed by 5 to 10 digits) from URL, text, or pure number.
    e.g. 'https://www.imdb.com/title/tt5104604/' -> 'tt5104604'
         'tt5104604' -> 'tt5104604'
         '5104604' -> 'tt5104604'
    """
    if not raw_id:
        return None
    raw = str(raw_id).strip()
    m = re.search(r'(tt\d{5,10})', raw, re.IGNORECASE)
    if m:
        return m.group(1).lower()
    m_num = re.search(r'^\d{5,10}$', raw)
    if m_num:
        return f"tt{raw}"
    return None


def fetch_cinemeta_details(imdb_id: str) -> dict | None:
    """
    Query Cinemeta (free, zero API key) for verified movie or series metadata by IMDb ID.
    Returns structured dict with title, year, poster_url, backdrop_url, overview,
    imdb_rating, runtime, genres, cast, director, and trailer.
    """
    clean_id = normalize_imdb_id(imdb_id)
    if not clean_id:
        return None

    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    for mtype in ('movie', 'series'):
        try:
            url = f'https://v3-cinemeta.strem.io/meta/{mtype}/{clean_id}.json'
            resp = requests.get(url, headers=headers, timeout=7)
            if resp.status_code == 200:
                meta = resp.json().get('meta')
                if meta and meta.get('name'):
                    runtime_int = None
                    raw_rt = meta.get('runtime')
                    if raw_rt:
                        m_rt = re.search(r'(\d+)', str(raw_rt))
                        if m_rt:
                            runtime_int = int(m_rt.group(1))

                    year_int = None
                    raw_year = meta.get('year') or meta.get('releaseInfo')
                    if raw_year:
                        m_y = re.search(r'(\d{4})', str(raw_year))
                        if m_y:
                            year_int = int(m_y.group(1))

                    rating_float = None
                    raw_rate = meta.get('imdbRating')
                    if raw_rate:
                        try:
                            rating_float = float(raw_rate)
                        except (ValueError, TypeError):
                            pass

                    trailer_key = None
                    trailer_url = None
                    trailer_streams = meta.get('trailerStreams') or []
                    if trailer_streams and trailer_streams[0].get('ytId'):
                        trailer_key = trailer_streams[0]['ytId']
                    elif meta.get('trailers'):
                        trailer_key = meta['trailers'][0].get('source')
                    if trailer_key:
                        trailer_url = f"https://www.youtube.com/watch?v={trailer_key}"

                    cast_list = []
                    for c in (meta.get('cast') or [])[:6]:
                        if isinstance(c, str):
                            cast_list.append({'name': c, 'character': '', 'profile_path': None})
                        elif isinstance(c, dict):
                            cast_list.append(c)

                    director_obj = {}
                    directors = meta.get('director') or []
                    if directors and isinstance(directors, list):
                        director_obj = {'name': directors[0], 'profile_path': None}
                    elif isinstance(directors, str):
                        director_obj = {'name': directors, 'profile_path': None}

                    genres = meta.get('genres') or meta.get('genre') or []
                    if isinstance(genres, str):
                        genres = [g.strip() for g in genres.split(',') if g.strip()]

                    return {
                        'imdb_id': clean_id,
                        'title': meta.get('name'),
                        'year': year_int,
                        'release_date': meta.get('released') or (f"{year_int}-01-01" if year_int else None),
                        'poster_url': meta.get('poster'),
                        'backdrop_url': meta.get('background'),
                        'overview': meta.get('description', ''),
                        'imdb_rating': rating_float,
                        'runtime': runtime_int,
                        'genres': genres,
                        'genres_str': ', '.join(genres) if genres else '',
                        'cast': cast_list,
                        'director': director_obj,
                        'trailer_key': trailer_key,
                        'trailer_url': trailer_url,
                        'moviedb_id': meta.get('moviedb_id'),
                    }
        except Exception:
            continue
    return None


def fetch_imdb_rating_and_id(title: str, year=None, known_imdb_id: str = None) -> tuple[float | None, str | None, list]:
    """
    Fetch authentic IMDb rating, IMDb ID (tt...), and genres using Cinemeta and IMDb suggestion API.
    Zero API key required, fast and accurate.
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    def query_cinemeta(imdb_id: str) -> tuple[float | None, list, bool]:
        """Fetch rating and genres from Cinemeta for a verified IMDb ID."""
        clean_id = normalize_imdb_id(imdb_id)
        if not clean_id:
            return None, [], False
        try:
            r = requests.get(f'https://v3-cinemeta.strem.io/meta/movie/{clean_id}.json', headers=headers, timeout=6)
            if r.status_code == 200:
                meta = r.json().get('meta', {})
                if meta and (meta.get('name') or meta.get('id') or meta.get('imdb_id')):
                    rate = meta.get('imdbRating')
                    rate_float = None
                    if rate:
                        try:
                            rate_float = float(rate)
                        except (ValueError, TypeError):
                            pass
                    genres = meta.get('genres') or meta.get('genre') or []
                    if isinstance(genres, str):
                        genres = [g.strip() for g in genres.split(',') if g.strip()]
                    return rate_float, genres, True
        except Exception:
            pass
        return None, [], False

    # 1. If known IMDb ID is provided (e.g. from TMDB or user)
    clean_known = normalize_imdb_id(known_imdb_id)
    if clean_known:
        rate, genres, found = query_cinemeta(clean_known)
        # CRITICAL: Never discard a known IMDb ID to fall through to fuzzy search!
        # Even if unrated or new, the known ID is the authoritative target.
        return rate, clean_known, genres

    # 2. Prepare title for search
    raw_clean = re.sub(r'[/\\:_#-]+', ' ', title or '')
    clean = re.sub(r'[^a-zA-Z0-9\s]+', ' ', raw_clean).strip().lower()
    q = re.sub(r'\s+', '_', clean)
    if not q:
        return None, None, []

    prefix = q[0] if q else 'x'
    # Try titles endpoint first (filters out persons/cast), fallback to general suggestion
    endpoints = [
        f'https://v3.sg.media-imdb.com/suggestion/titles/{prefix}/{q}.json',
        f'https://v3.sg.media-imdb.com/suggestion/{prefix}/{q}.json',
        f'https://v3.sg.media-imdb.com/suggestion/x/{q}.json',
    ]

    entries = []
    for ep in endpoints:
        try:
            r = requests.get(ep, headers=headers, timeout=6)
            if r.status_code == 200:
                data = r.json().get('d', [])
                if data:
                    entries = data
                    break
        except Exception:
            continue

    if not entries:
        return None, None, []

    # Filter for movie/media candidates with tt... IDs
    candidates = [
        item for item in entries
        if item.get('id', '').startswith('tt')
        and item.get('q') in ('feature', 'TV movie', 'movie', 'short', 'tvSeries', 'tvMiniSeries', None)
    ]

    # Helper to check a candidate
    def check_candidate(item):
        imdb_id = item.get('id')
        if imdb_id and imdb_id.startswith('tt'):
            rate, genres, found = query_cinemeta(imdb_id)
            if found or rate is not None:
                return rate, imdb_id, genres
        return None, None, []

    # Pass 1: exact or adjacent year match (within +/- 1 year)
    if year:
        try:
            target_year = int(year)
            for item in candidates:
                item_year = item.get('y')
                if item_year:
                    try:
                        if abs(int(item_year) - target_year) <= 1:
                            rate, i_id, g_list = check_candidate(item)
                            if rate is not None:
                                return rate, i_id, g_list
                    except (ValueError, TypeError):
                        pass
        except (ValueError, TypeError):
            pass

    # Pass 2: best candidate without strict year matching
    for item in candidates:
        rate, i_id, g_list = check_candidate(item)
        if rate is not None:
            return rate, i_id, g_list

    # Pass 3: Fallback to TMDB external IDs if IMDb suggestion API yielded no match
    tmdb_key = TMDB_API_KEY or os.getenv('TMDB_API_KEY')
    if tmdb_key:
        try:
            params = {'api_key': tmdb_key, 'query': clean_title_for_search(title)}
            if year:
                params['year'] = str(year)
            r_tmdb = requests.get('https://api.themoviedb.org/3/search/movie', params=params, timeout=5)
            if r_tmdb.status_code == 200:
                t_results = r_tmdb.json().get('results', [])
                if t_results:
                    tmdb_m_id = t_results[0].get('id')
                    if tmdb_m_id:
                        r_ext = requests.get(
                            f'https://api.themoviedb.org/3/movie/{tmdb_m_id}/external_ids',
                            params={'api_key': tmdb_key},
                            timeout=5
                        )
                        if r_ext.status_code == 200:
                            found_tt = r_ext.json().get('imdb_id')
                            if found_tt and found_tt.startswith('tt'):
                                rate, genres = query_cinemeta(found_tt)
                                if rate is not None:
                                    return rate, found_tt, genres
        except Exception:
            pass

    return None, None, []


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
