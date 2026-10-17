"""Run the unchanged paper recorder with a bounded background writer and health log.

No order API, strategy change or source-clock substitution. This is a new
collector runtime identity; do not silently inherit an older frozen protocol.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import time
from pathlib import Path
import paper_trader as paper
from buffered_recorder import BufferedRecorder


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.pending')
    temporary.write_text(json.dumps(value, sort_keys=True, allow_nan=False) + '\n')
    temporary.replace(path)


class SupervisedTrader(paper.LiveTrader):
    async def writer_watch(self):
        due = time.monotonic() + 1
        ticks = 0
        while True:
            await asyncio.sleep(max(0, due - time.monotonic()))
            lag_ms = max(0.0, (time.monotonic() - due) * 1000)
            due = time.monotonic() + 1
            self.rec.check()
            ticks += 1
            if ticks % 10 == 0:
                row = dict(at=time.time_ns() / 1_000_000, where='capture-heartbeat',
                           clob_active=self.clob_active, clob_epoch=self.clob_epoch,
                           event_loop_lag_ms=lag_ms, writer=self.rec.stats())
                self.rec.write('capture-health', row)
                # Heartbeats distinguish process life, writes and connection state.
                # They do NOT certify message completeness or per-token freshness.
                await asyncio.to_thread(atomic_json, self.out / 'capture-health.json', row)

    async def run(self, hours):
        if not 0 < hours <= 24 * 366:
            raise ValueError('invalid recording duration')
        meta = dict(schema='buffered-capture-v1', started_ms=time.time_ns()/1_000_000,
                    run_id=os.environ.get('GITHUB_RUN_ID'),
                    source_sha256=hashlib.sha256(Path(paper.__file__).read_bytes()).hexdigest(),
                    wrapper_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    writer_sha256=hashlib.sha256(Path(__file__).with_name('buffered_recorder.py').read_bytes()).hexdigest(),
                    collector_region='unverified', complete=False, feed_health_verified=False)
        atomic_json(self.out / 'capture-runtime.json', meta)
        capture = asyncio.create_task(super().run(hours))
        watch = asyncio.create_task(self.writer_watch())
        try:
            done, _ = await asyncio.wait([capture, watch], return_when=asyncio.FIRST_COMPLETED)
            if capture not in done:
                await watch  # propagate fatal I/O/backpressure
                raise RuntimeError('writer supervisor unexpectedly stopped')
            await capture
            self.rec.check()
            meta['complete'] = True
        except BaseException as exc:
            meta['error'] = repr(exc)
            raise
        finally:
            for task in (capture, watch):
                if not task.done():
                    task.cancel()
            await asyncio.gather(capture, watch, return_exceptions=True)
            try:
                self.rec.close()
            except BaseException as exc:
                meta.update(complete=False, error=repr(exc))
                raise
            finally:
                meta.update(ended_ms=time.time_ns()/1_000_000, writer=self.rec.stats())
                atomic_json(self.out / 'capture-runtime.json', meta)


def main(argv=None):
    # Restore names on return: importing this module must not alter other tests.
    recorder, trader = paper.Recorder, paper.LiveTrader
    paper.Recorder, paper.LiveTrader = BufferedRecorder, SupervisedTrader
    try:
        return paper.main(argv)
    finally:
        paper.Recorder, paper.LiveTrader = recorder, trader


if __name__ == '__main__':
    main()
