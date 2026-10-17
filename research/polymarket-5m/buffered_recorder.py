"""Bounded FIFO gzip writer. Receipt stamps and JSON snapshots are taken by the caller.

Only gzip and file I/O move to the worker. Queue exhaustion and I/O failure are
fatal, never silently dropped or described as complete recordings.
"""
from __future__ import annotations
import gzip
import json
import queue
import re
import threading
import time
from pathlib import Path


class BufferedRecorder:
    def __init__(self, out, flush_every=1.0, max_pending_bytes=64 * 1024 * 1024):
        if flush_every <= 0 or max_pending_bytes <= 0:
            raise ValueError('positive flush interval and byte budget required')
        self.out = Path(out)
        self.flush_every = float(flush_every)
        self.max_pending_bytes = int(max_pending_bytes)
        self._queue = queue.Queue(maxsize=65536)
        self._lock = threading.Lock()
        self._pending = self._accepted = self._written = 0
        self._flushed = 0
        self._error = None
        self._closing = False
        self._last_flush_ms = None
        self._latest = {}
        self._thread = threading.Thread(target=self._work, name='record-gzip-writer', daemon=True)
        self._thread.start()

    def check(self):
        with self._lock:
            error = self._error
        if error:
            raise RuntimeError('recording writer failed: ' + error)

    def stats(self):
        with self._lock:
            return dict(accepted=self._accepted, written=self._written, flushed=self._flushed,
                        pending_bytes=self._pending, last_flush_ms=self._last_flush_ms,
                        error=self._error, latest_stream_enqueue_ms=dict(self._latest))

    def write(self, name, obj):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in ('.', '..'):
            raise ValueError('invalid recording stream name')
        at = time.time_ns() / 1_000_000
        if name == 'markets' and isinstance(obj, dict) and '_recorded_at_ms' not in obj:
            obj = {**obj, '_recorded_at_ms': at}
        # Serialize here: callers may mutate a book after this call returns.
        data = (json.dumps(obj, separators=(',', ':')) + '\n').encode('utf-8')
        day = time.strftime('%Y-%m-%d', time.gmtime(at / 1000))
        with self._lock:
            if self._error or self._closing:
                raise RuntimeError('recording writer unavailable: ' + str(self._error or 'closing'))
            if self._pending + len(data) > self.max_pending_bytes:
                self._error = 'bounded recording queue exhausted; capture must fail'
                raise RuntimeError(self._error)
            try:
                self._queue.put_nowait((day, name, data))
            except queue.Full:
                self._error = 'bounded recording item queue exhausted; capture must fail'
                raise RuntimeError(self._error)
            self._pending += len(data)
            self._accepted += 1
            self._latest[name] = at

    def _work(self):
        files = {}
        flushed_at = time.monotonic()
        def flush():
            nonlocal flushed_at
            for handle in files.values():
                handle.flush()
            with self._lock:
                self._flushed = self._written
                self._last_flush_ms = time.time_ns() / 1_000_000
            flushed_at = time.monotonic()
        try:
            while True:
                try:
                    item = self._queue.get(timeout=self.flush_every)
                except queue.Empty:
                    flush()
                    continue
                if item is None:
                    flush()
                    break
                day, name, data = item
                key = (day, name)
                if key not in files:
                    for old in [k for k in files if k[1] == name]:
                        files.pop(old).close()
                    path = self.out / 'raw' / day / (name + '.jsonl.gz')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    files[key] = gzip.open(path, 'ab', compresslevel=1)
                files[key].write(data)
                with self._lock:
                    self._pending -= len(data)
                    self._written += 1
                if time.monotonic() - flushed_at >= self.flush_every:
                    flush()
        except BaseException as exc:
            with self._lock:
                self._error = repr(exc)
        finally:
            for handle in files.values():
                try:
                    handle.close()
                except BaseException as exc:
                    with self._lock:
                        self._error = self._error or repr(exc)

    def close(self):
        with self._lock:
            first = not self._closing
            self._closing = True
        deadline = time.monotonic() + 60
        if first and self._thread.is_alive():
            while self._thread.is_alive():
                if time.monotonic() >= deadline:
                    raise RuntimeError('recording writer queue did not drain within 60 seconds')
                try:
                    self._queue.put(None, timeout=.1)
                    break
                except queue.Full:
                    continue
        self._thread.join(timeout=60)
        if self._thread.is_alive():
            raise RuntimeError('recording writer did not drain within 60 seconds')
        self.check()
        if self.stats()['pending_bytes']:
            raise RuntimeError('undrained recording bytes')
