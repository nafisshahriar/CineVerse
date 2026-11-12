import threading
import time
import io
import re
import sys
from collections import deque
from datetime import datetime
from django.core.management import call_command
from django.db import connection, connections
from .cache_utils import invalidate_catalog_cache


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
        self.state_lock = threading.RLock()

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
        if not line:
            return
        # Clean ANSI terminal escape codes if present
        clean_line = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', line).strip()
        if not clean_line:
            return
        with self.state_lock:
            timestamp = datetime.now().strftime('%H:%M:%S')
            self.logs.append(f"[{timestamp}] {clean_line}")
            self._parse_progress_from_line(clean_line)

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

        # Server connection / starting indicator
        conn_match = re.search(r'(?:Connecting to|Starting crawl at)\s*([^\n\r]+)', line)
        if conn_match:
            self.task_state['current_item'] = f"Connecting: {conn_match.group(1).strip()}"
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

        init_item = f"Connecting to {params.get('url')}..." if (params and params.get('url')) else f"Starting {task_name}..."

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
                'current_item': init_item,
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
            is_stopped = False
            with self.state_lock:
                if self.stop_requested.is_set():
                    is_stopped = True
                    self.task_state['status'] = 'stopped'
                    self.task_state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                elif self.task_state['status'] in ('running', 'stopping'):
                    self.task_state['status'] = 'completed'
                    self.task_state['progress_percent'] = 100.0
                    self.task_state['progress_total'] = max(self.task_state['progress_total'], self.task_state['progress_current'])
                    self.task_state['remaining'] = 0
                    self.task_state['eta'] = '0s'
                    self.task_state['current_item'] = 'Complete - All items up to date'
                    self.task_state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

            if is_stopped:
                self.append_log("[STOPPED] Task was stopped by administrator.")
            else:
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
            invalidate_catalog_cache()

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
            missing_details = bool(params.get('missing_details', False))

            kwargs = {
                'workers': workers,
                'force': force,
                'include_details': include_details,
                'missing_details': missing_details,
                'stdout': out_stream,
                'stderr': out_stream,
            }
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
        from movies.services import reconcile_copy_groups

        reconcile_copy_groups(
            group_keys=None,
            stop_check=self.is_stop_requested,
            log_fn=stream.write,
        )


task_manager = BackgroundTaskManager()


def is_task_stopped() -> bool:
    """Helper to check if the active background task has received a cancellation signal."""
    return task_manager.stop_requested.is_set()

