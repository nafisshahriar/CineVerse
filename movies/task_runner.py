import threading
import time
import io
import re
import sys
from collections import deque
from datetime import datetime
from django.core.management import call_command
from django.db import connection, connections


class TaskOutput(io.StringIO):
    """Custom stream to capture stdout/stderr in real-time and extract progress."""

    def __init__(self, callback=None):
        super().__init__()
        self.callback = callback
        self._buffer = ""

    def write(self, s):
        super().write(s)
        if not s:
            return
        self._buffer += s
        while "\n" in self._buffer or "\r" in self._buffer:
            # Split on either newline or carriage return for progress updates
            line, _, remainder = self._buffer.partition("\n")
            if "\r" in line:
                parts = line.split("\r")
                line = parts[-1]
            self._buffer = remainder
            line = line.strip()
            if line and self.callback:
                self.callback(line)

    def flush(self):
        super().flush()
        if self._buffer and self.callback:
            line = self._buffer.strip()
            if line:
                self.callback(line)
            self._buffer = ""


class BackgroundTaskManager:
    """Thread-safe background task manager for CineVerse maintenance commands."""

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._init()
            return cls._instance

    def _init(self):
        self.current_thread = None
        self.stop_requested = threading.Event()
        self.task_state = {
            'task_id': None,
            'task_type': None,
            'task_name': 'No active task',
            'status': 'idle',  # 'idle', 'running', 'completed', 'failed', 'stopped'
            'progress_current': 0,
            'progress_total': 0,
            'progress_percent': 0.0,
            'current_item': '',
            'remaining': 0,
            'speed': '',
            'eta': '',
            'started_at': None,
            'finished_at': None,
            'error_message': None,
        }
        self.logs = deque(maxlen=400)
        self.state_lock = threading.Lock()

    def is_running(self):
        with self.state_lock:
            return self.task_state['status'] == 'running' and self.current_thread is not None and self.current_thread.is_alive()

    def get_status(self):
        with self.state_lock:
            # Check if thread died without setting completed/failed
            if self.task_state['status'] == 'running':
                if self.current_thread is None or not self.current_thread.is_alive():
                    self.task_state['status'] = 'completed' if not self.task_state.get('error_message') else 'failed'
                    self.task_state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            logs_list = list(self.logs)
            state_copy = dict(self.task_state)
            state_copy['logs'] = logs_list
            return state_copy

    def append_log(self, line: str):
        with self.state_lock:
            timestamp = datetime.now().strftime('%H:%M:%S')
            self.logs.append(f"[{timestamp}] {line}")
            self._parse_progress_from_line(line)

    def _parse_progress_from_line(self, line: str):
        """Parse progress markers like [120/500 (24%) remaining: 380] or [1/5] ( 20.0%) | 4 left | ETA: 00m 04s."""
        # Format A: [1/5] ( 20.0%) | 4 left | ETA: 00m 04s (1.0/s)
        match_a = re.search(r'\[(\d+)/(\d+)\]\s*\(\s*([\d\.]+)%\)\s*\|\s*(\d+)\s*left\s*\|\s*ETA:\s*([^\|\(]+)(?:\(([^/]+)/s\))?', line)
        if match_a:
            curr = int(match_a.group(1))
            tot = int(match_a.group(2))
            pct = float(match_a.group(3))
            rem = int(match_a.group(4))
            eta = match_a.group(5).strip() if match_a.group(5) else ''
            spd = f"{match_a.group(6)} it/s" if match_a.group(6) else ''

            self.task_state['progress_current'] = curr
            self.task_state['progress_total'] = tot
            self.task_state['progress_percent'] = pct
            self.task_state['remaining'] = rem
            self.task_state['speed'] = spd
            self.task_state['eta'] = eta
            return

        # Format B: [120/500 (24%) remaining: 380 speed: 12.5 it/s ETA: 30s]
        match_b = re.search(r'\[(\d+)/(\d+)\s*\(([\d\.]+)%\)\s*(?:remaining:\s*(\d+))?\s*(?:speed:\s*([\d\.]+\s*it/s))?\s*(?:ETA:\s*([^\]]+))?\]', line)
        if match_b:
            curr = int(match_b.group(1))
            tot = int(match_b.group(2))
            pct = float(match_b.group(3))
            rem = int(match_b.group(4)) if match_b.group(4) else (tot - curr)
            spd = match_b.group(5) or ''
            eta = match_b.group(6) or ''

            self.task_state['progress_current'] = curr
            self.task_state['progress_total'] = tot
            self.task_state['progress_percent'] = pct
            self.task_state['remaining'] = rem
            self.task_state['speed'] = spd
            self.task_state['eta'] = eta.strip()
            return

        # Format C: Crawl dynamic scanner stream:
        # e.g. [CRAWL 45] Scanned: 45 | New: 2 | Skipped: 43 | Checking: Inception (2010)
        # or [45/100] ( 45.0%) | Scanned: 45 | New: 2 | Skipped: 43 | Checking: Inception (2010)
        match_c = re.search(r'(?:\[(?:CRAWL\s*)?(\d+)(?:/(\d+))?\](?:\s*\(\s*([\d\.]+)%\))?\s*\|\s*)?Scanned:\s*(\d+)(?:\s*\|\s*New:\s*(\d+))?(?:\s*\|\s*Updated:\s*(\d+))?(?:\s*\|\s*Skipped:\s*(\d+))?(?:\s*\|\s*Checking:\s*([^\|\n\r]+))?', line)
        if match_c and (match_c.group(1) or match_c.group(4)):
            scanned = int(match_c.group(4) or match_c.group(1))
            tot = int(match_c.group(2)) if match_c.group(2) else None
            pct = float(match_c.group(3)) if match_c.group(3) else None

            self.task_state['progress_current'] = scanned
            if tot:
                self.task_state['progress_total'] = tot
                self.task_state['progress_percent'] = pct if pct is not None else min(100.0, (scanned / tot) * 100)
                self.task_state['remaining'] = max(0, tot - scanned)
            else:
                if self.task_state['progress_total'] < scanned:
                    self.task_state['progress_total'] = scanned
                self.task_state['progress_percent'] = 0.0

            checking = match_c.group(8)
            if checking:
                self.task_state['current_item'] = checking.strip()

            if self.task_state.get('started_at'):
                try:
                    start_dt = datetime.strptime(self.task_state['started_at'], '%Y-%m-%d %H:%M:%S')
                    elapsed = max(0.5, (datetime.now() - start_dt).total_seconds())
                    spd = scanned / elapsed
                    self.task_state['speed'] = f"{spd:.1f} it/s"
                    if tot and spd > 0:
                        rem_sec = int((tot - scanned) / spd)
                        self.task_state['eta'] = f"{rem_sec // 60}m {rem_sec % 60:02d}s"
                except Exception:
                    pass
            return

        # Item indicator: e.g. "Processing: Inception (2010)" or "[OK] Title" or "[SKIPPED] Title"
        item_match = re.search(r'(?:Processing:|\[OK\]|\[FAIL\]|\[SKIPPED\]|\[SKIP\]|Processed:)\s*([^\(\n\r]+)', line)
        if item_match:
            self.task_state['current_item'] = item_match.group(1).strip()

    def start_task(self, task_type: str, params: dict = None) -> tuple[bool, str]:
        if self.is_running():
            return False, f"A task is already running: {self.task_state.get('task_name', 'Task')}"

        params = params or {}
        self.stop_requested.clear()

        task_names = {
            'crawl': 'H5AI Crawl & Movie Import',
            'fetch_posters': 'Fetch Missing Posters & TMDB Metadata',
            'fetch_missing_metadata': 'Fetch Missing Posters & TMDB Metadata',
            'fetch_genres': 'Multi-Threaded Genre Fetch',
            'fetch_imdb': 'IMDb Ratings & IDs Sync',
            'refresh_metadata': 'TMDB Metadata Refresh',
            'backfill_metadata': 'Backfill Missing Metadata',
            'regroup_copies': 'Recalculate Groups & Primary Copies',
            'update_all': 'Full Automated Pipeline',
            'reset_catalog': 'Reset Catalog & Start Fresh',
        }

        task_name = task_names.get(task_type, f"Task: {task_type}")

        with self.state_lock:
            self.logs.clear()
            self.task_state = {
                'task_id': f"{task_type}_{int(time.time())}",
                'task_type': task_type,
                'task_name': task_name,
                'status': 'running',
                'progress_current': 0,
                'progress_total': 0,
                'progress_percent': 0.0,
                'current_item': 'Initializing...',
                'remaining': 0,
                'speed': '',
                'eta': '',
                'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'finished_at': None,
                'error_message': None,
            }

        self.append_log(f"Starting {task_name}...")
        if params:
            self.append_log(f"Parameters: {params}")

        thread = threading.Thread(
            target=self._run_task_wrapper,
            args=(task_type, params),
            daemon=True
        )
        self.current_thread = thread
        thread.start()
        return True, "Task started successfully"

    def stop_task(self) -> tuple[bool, str]:
        if not self.is_running():
            return False, "No active task is running."

        self.stop_requested.set()
        self.append_log("[STOP] Cancellation requested by administrator. Halting background workers...")
        with self.state_lock:
            self.task_state['status'] = 'stopping'
            self.task_state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return True, "Cancellation signal sent. Workers are terminating."

    def is_stop_requested(self) -> bool:
        return self.stop_requested.is_set()

    def _run_task_wrapper(self, task_type: str, params: dict):
        try:
            connections.close_all()
            self._execute_task(task_type, params)
            with self.state_lock:
                if self.stop_requested.is_set():
                    self.task_state['status'] = 'stopped'
                    self.task_state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self.append_log("[STOPPED] Task was stopped by administrator.")
                elif self.task_state['status'] in ('running', 'stopping'):
                    self.task_state['status'] = 'completed'
                    self.task_state['progress_percent'] = 100.0
                    self.task_state['progress_total'] = max(self.task_state['progress_total'], self.task_state['progress_current'])
                    self.task_state['remaining'] = 0
                    self.task_state['eta'] = '0s'
                    self.task_state['current_item'] = 'Complete - All items up to date'
                    self.task_state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    self.append_log("[SUCCESS] Task completed successfully!")
        except Exception as e:
            import traceback
            err = str(e)
            tb = traceback.format_exc()
            with self.state_lock:
                self.task_state['status'] = 'failed'
                self.task_state['error_message'] = err
                self.task_state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            self.append_log(f"[ERROR] Task failed: {err}")
            self.append_log(tb)
        finally:
            connections.close_all()

    def _execute_task(self, task_type: str, params: dict):
        out_stream = TaskOutput(callback=self.append_log)

        if task_type == 'crawl':
            url = params.get('url', '').strip()
            if not url:
                raise ValueError("URL is required for crawling")
            source = params.get('source', '').strip()
            force = bool(params.get('force', False))
            retry_failed = bool(params.get('retry_failed', False))
            max_items = params.get('max_items')
            max_items = int(max_items) if max_items and int(max_items) > 0 else None

            kwargs = {'url': url, 'force': force, 'retry_failed': retry_failed, 'stdout': out_stream, 'stderr': out_stream}
            if source:
                kwargs['source'] = source
            if max_items:
                kwargs['max_items'] = max_items
            call_command('crawl_h5ai', **kwargs)

        elif task_type in ('fetch_posters', 'fetch_missing_metadata'):
            limit = int(params.get('limit', 0)) if params.get('limit') else None
            workers = int(params.get('workers', 4)) if params.get('workers') else 4
            force = bool(params.get('force', False))
            include_details = bool(params.get('include_details', False))

            kwargs = {'workers': workers, 'force': force, 'include_details': include_details, 'stdout': out_stream, 'stderr': out_stream}
            if limit and limit > 0:
                kwargs['limit'] = limit
            call_command('fetch_missing_metadata', **kwargs)

        elif task_type == 'fetch_genres':
            limit = int(params.get('limit', 500)) if params.get('limit') else 500
            fetch_all = bool(params.get('all', False))
            workers = int(params.get('workers', 4)) if params.get('workers') else 4
            force = bool(params.get('force', False))

            kwargs = {'workers': workers, 'force': force, 'stdout': out_stream, 'stderr': out_stream}
            if fetch_all:
                kwargs['all'] = True
            else:
                kwargs['limit'] = limit
            call_command('fetch_genres', **kwargs)

        elif task_type == 'fetch_imdb':
            limit = int(params.get('limit', 500)) if params.get('limit') else 500
            fetch_all = bool(params.get('all', False))
            workers = int(params.get('workers', 4)) if params.get('workers') else 4
            force = bool(params.get('force', False))

            kwargs = {'workers': workers, 'force': force, 'stdout': out_stream, 'stderr': out_stream}
            if fetch_all:
                kwargs['all'] = True
            else:
                kwargs['limit'] = limit
            call_command('fetch_imdb_ratings', **kwargs)

        elif task_type == 'refresh_metadata':
            changes = bool(params.get('changes', False))
            days = int(params.get('days', 7)) if params.get('days') else 7
            stale = bool(params.get('stale', False))
            limit = int(params.get('limit', 100)) if params.get('limit') else None
            delay = float(params.get('delay', 0.2)) if params.get('delay') else 0.2

            kwargs = {'delay': delay, 'stdout': out_stream, 'stderr': out_stream}
            if changes:
                kwargs['changes'] = True
                kwargs['days'] = days
            elif stale:
                kwargs['stale'] = True
            if limit:
                kwargs['limit'] = limit
            call_command('refresh_metadata', **kwargs)

        elif task_type == 'backfill_metadata':
            batch_size = int(params.get('batch_size', 500)) if params.get('batch_size') else 500
            force = bool(params.get('force', False))
            call_command('backfill_metadata', batch_size=batch_size, force=force, stdout=out_stream, stderr=out_stream)

        elif task_type == 'regroup_copies':
            self._recalculate_copy_groups(out_stream)

        elif task_type == 'update_all':
            call_command('update_all', stdout=out_stream, stderr=out_stream)

        elif task_type == 'reset_catalog':
            crawl_url = params.get('crawl_url', '').strip() or None
            kwargs = {'yes': True, 'stdout': out_stream, 'stderr': out_stream}
            if crawl_url:
                kwargs['crawl'] = crawl_url
            call_command('reset_catalog', **kwargs)

        else:
            raise ValueError(f"Unknown task type: {task_type}")

    def _recalculate_copy_groups(self, stream):
        """Re-evaluates clean titles, group keys, and primary copies across the catalog."""
        from movies.models import Movie
        from movies.utils import parse_movie_info, clean_title_for_search
        import os
        from urllib.parse import unquote

        stream.write("Analyzing and regrouping movies across all servers...")
        movies = list(Movie.objects.all())
        total = len(movies)
        stream.write(f"Total files in catalog: {total}")

        # Group copies by group_key
        groups = {}
        for idx, m in enumerate(movies):
            if self.stop_requested.is_set():
                stream.write("[STOPPED] Regrouping cancelled by user.")
                return

            if idx % 1000 == 0:
                pct = round((idx / total) * 100, 1)
                stream.write(f"[{idx}/{total} ({pct}%)] Grouping movies...")

            file_name = os.path.basename(unquote(m.file_url or ''))
            dir_name = os.path.basename(unquote(m.directory_url or '').rstrip('/\\'))
            info = parse_movie_info(dir_name or file_name, file_name)

            if not m.clean_title:
                m.clean_title = info.get('clean_title') or m.title
            if not m.group_key:
                m.group_key = info.get('group_key', '')
            if not m.resolution and info.get('resolution'):
                m.resolution = info.get('resolution')
            if not m.source and info.get('source'):
                m.source = info.get('source')

            key = m.group_key or f"{m.clean_title.lower()}:::{m.year or ''}"
            groups.setdefault(key, []).append(m)

        stream.write(f"Found {len(groups)} unique movie groups across {total} files.")
        stream.write("Assigning primary copies (prioritizing 4K > 1080P > 720P)...")

        res_priority = {'4k': 4, '1080p': 3, '720p': 2, '480p': 1}
        movies_to_update = []

        for key, copies in groups.items():
            # Sort copies by resolution score desc, then id
            copies.sort(
                key=lambda c: (
                    res_priority.get((c.resolution or '').lower(), 0),
                    1 if c.source else 0,
                    -c.id
                ),
                reverse=True
            )
            # First is primary, rest are secondary
            for i, c in enumerate(copies):
                should_be_primary = (i == 0)
                if c.is_primary_copy != should_be_primary:
                    c.is_primary_copy = should_be_primary
                    movies_to_update.append(c)

        if movies_to_update:
            stream.write(f"Updating {len(movies_to_update)} movie copy flags in database...")
            # Batch update in chunks of 500
            for i in range(0, len(movies_to_update), 500):
                chunk = movies_to_update[i:i+500]
                Movie.objects.bulk_update(chunk, ['is_primary_copy', 'clean_title', 'group_key', 'resolution', 'source'])
            stream.write(f"[OK] Successfully updated primary copy flags for {len(movies_to_update)} movies.")
        else:
            stream.write("[OK] All copy groups are already optimal and up-to-date!")


task_manager = BackgroundTaskManager()


def is_task_stopped() -> bool:
    """Helper to check if the active background task has received a cancellation signal."""
    return task_manager.stop_requested.is_set()

