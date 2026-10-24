"""Offline recorder/writer/handoff acceptance; never opens a market connection."""
from __future__ import annotations
import asyncio
import gzip
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import tarfile
import unittest
from unittest import mock

from buffered_recorder import BufferedRecorder
import capture_checkpoint as checkpoint
import record_live_buffered as live


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.root = Path(self.tmp.name)
    def tearDown(self):
        self.tmp.cleanup()
    def rows(self, stream):
        result=[]
        for p in sorted(self.root.glob(f'raw/*/{stream}.jsonl.gz')):
            with gzip.open(p, 'rt') as f:
                result += [json.loads(line) for line in f]
        return result
    def test_fifo_and_complete_gzip_footer(self):
        r=BufferedRecorder(self.root)
        for i in range(20000):
            r.write('clob-btc', dict(recv_ms=i, sequence=i, msg={'price':'.51'}))
        r.close()
        self.assertEqual([x['sequence'] for x in self.rows('clob-btc')], list(range(20000)))
        s=r.stats(); self.assertEqual((s['accepted'],s['written'],s['flushed'],s['pending_bytes']), (20000,20000,20000,0))
    def test_snapshot_before_caller_mutation(self):
        r=BufferedRecorder(self.root); obj={'recv_ms':123,'book':[[.5,100]]}
        r.write('clob-btc',obj); obj['book'][0][1]=0; r.close()
        self.assertEqual(self.rows('clob-btc')[0]['book'][0][1],100)
    def test_metadata_local_clock_preserved(self):
        r=BufferedRecorder(self.root)
        with mock.patch('buffered_recorder.time.time_ns',return_value=1_700_000_000_123_000_000):
            r.write('markets',{'slug':'x'})
            r.write('markets',{'slug':'y','_recorded_at_ms':12})
        r.close(); a,b=self.rows('markets')
        self.assertEqual(a['_recorded_at_ms'],1_700_000_000_123)
        self.assertEqual(b['_recorded_at_ms'],12)
    def test_day_partition_taken_at_enqueue(self):
        r=BufferedRecorder(self.root)
        with mock.patch('buffered_recorder.time.time_ns',return_value=86_399_000_000_000): r.write('x',{'i':1})
        with mock.patch('buffered_recorder.time.time_ns',return_value=86_401_000_000_000): r.write('x',{'i':2})
        r.close()
        self.assertEqual(sorted(p.parent.name for p in self.root.glob('raw/*/x.jsonl.gz')),['1970-01-01','1970-01-02'])
    def test_capacity_failure_is_explicit_not_drop(self):
        r=BufferedRecorder(self.root,max_pending_bytes=2)
        with self.assertRaisesRegex(RuntimeError,'exhausted'): r.write('x',{'message':'too large'})
        with self.assertRaisesRegex(RuntimeError,'exhausted'): r.close()
        self.assertEqual(r.stats()['accepted'],0)
    def test_disk_failure_surfaces(self):
        with mock.patch('buffered_recorder.gzip.open',side_effect=OSError('disk full')):
            r=BufferedRecorder(self.root);r.write('x',{'i':1});r._thread.join(2)
            with self.assertRaisesRegex(RuntimeError,'disk full'):r.close()
            self.assertEqual(r.stats()['written'],0)
    def test_file_io_is_off_caller_thread(self):
        entered=threading.Event(); release=threading.Event()
        class Slow:
            def write(self,data): entered.set();release.wait(2)
            def flush(self): pass
            def close(self): pass
        with mock.patch('buffered_recorder.gzip.open',return_value=Slow()):
            r=BufferedRecorder(self.root);r.write('x',{'i':1})
            self.assertTrue(entered.wait(1))
            r.write('x',{'i':2})
            self.assertEqual(r.stats()['accepted'],2)
            release.set();r.close()
    def test_close_idempotent(self):
        r=BufferedRecorder(self.root);r.write('x',{'i':1});r.close();r.close()
        with self.assertRaisesRegex(RuntimeError,'closing'):r.write('x',{})
    def test_paths_rejected(self):
        r=BufferedRecorder(self.root)
        for name in ('../secret','a/b','..','.'):
            with self.assertRaises(ValueError):r.write(name,{})
        r.close()
    def test_invalid_configuration(self):
        with self.assertRaises(ValueError):BufferedRecorder(self.root,flush_every=0)


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.capture=self.root/'capture';self.capture.mkdir()
        r=BufferedRecorder(self.capture);r.write('clob-btc',{'recv_ms':123,'msg':{}});r.close()
        (self.capture/'.env').write_text('MUST_NOT_BE_ARCHIVED')
        (self.capture/'capture-runtime.json').write_text('{"complete":false}')
        self.archive=self.root/'capture.tar'
    def tearDown(self): self.tmp.cleanup()
    def pack(self,**kwargs):return checkpoint.pack(self.capture,self.archive,source_commit='a'*40,**kwargs)
    def test_roundtrip_and_no_secret_files(self):
        result=self.pack(capture_outcome='success')
        restored=self.root/'restored';value=checkpoint.restore(self.archive,restored)
        self.assertEqual(value['restored_files'],result['files'])
        self.assertFalse((restored/'.env').exists())
        self.assertFalse(value['complete_feed_health_verified'])
        for p in self.capture.glob('raw/*/*.gz'):
            self.assertEqual(p.read_bytes(),(restored/p.relative_to(self.capture)).read_bytes())
    def test_corrupt_archive_rejected(self):
        self.pack();self.archive.write_bytes(self.archive.read_bytes()+b'changed')
        with self.assertRaisesRegex(ValueError,'hash mismatch'):checkpoint.restore(self.archive,self.root/'restored')
    def test_existing_destination_rejected(self):
        self.pack()
        with self.assertRaisesRegex(ValueError,'already exists'):checkpoint.restore(self.archive,self.capture)
    def test_existing_archive_rejected(self):
        self.pack()
        with self.assertRaisesRegex(ValueError,'already exists'):self.pack()
    def test_symlink_rejected(self):
        p=self.capture/'raw'/'1970-01-01';p.mkdir(exist_ok=True,parents=True)
        (p/'evil.jsonl.gz').symlink_to(self.capture/'.env')
        with self.assertRaisesRegex(ValueError,'symlink'):self.pack()
    def test_size_cap_not_truncation(self):
        with self.assertRaisesRegex(ValueError,'byte cap'):self.pack(max_bytes=1)
        self.assertFalse(self.archive.exists())
    def test_empty_capture_not_success(self):
        empty=self.root/'empty';empty.mkdir()
        with self.assertRaisesRegex(ValueError,'no raw'):checkpoint.pack(empty,self.archive)
    def test_failed_capture_identity_preserved(self):
        self.pack(capture_outcome='failure')
        result=checkpoint.restore(self.archive,self.root/'restored')
        self.assertEqual(result['capture_outcome'],'failure')
    def test_gzip_validity_not_fabricated(self):
        next(self.capture.glob('raw/*/*.gz')).write_bytes(b'broken-stream')
        self.pack()
        with tarfile.open(self.archive) as t: m=json.load(t.extractfile('capture-manifest.json'))
        self.assertFalse(m['full_gzip_decode_verified'])
    def test_allowlist(self):
        for name in ('/tmp/bad','raw/../bad.jsonl.gz','raw/day/x/evil.jsonl.gz','.env','capture.tar'):
            self.assertFalse(checkpoint.allowed(name))
        self.assertTrue(checkpoint.allowed('raw/2026-10-17/errors.jsonl.gz'))
    def test_member_checksum_mismatch(self):
        self.pack()
        with tarfile.open(self.archive) as src:
            values=[(m.name,src.extractfile(m).read()) for m in src.getmembers()]
        with tarfile.open(self.archive,'w') as dst:
            for i,(name,data) in enumerate(values):
                if i==1:data=bytes([data[0]^1])+data[1:]
                info=tarfile.TarInfo(name);info.size=len(data);dst.addfile(info,io.BytesIO(data))
        self.archive.with_suffix('.tar.sha256').write_text(checkpoint.sha(self.archive)+'\n')
        with self.assertRaisesRegex(ValueError,'member hash'):checkpoint.restore(self.archive,self.root/'restored')


class SupervisionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.trader=live.SupervisedTrader.__new__(live.SupervisedTrader)
        self.trader.out=self.root;self.trader.rec=BufferedRecorder(self.root)
        self.trader.clob_active=False;self.trader.clob_epoch=0
    async def asyncTearDown(self):
        try:self.trader.rec.close()
        except RuntimeError:pass
        self.tmp.cleanup()
    async def test_normal_parent_completion(self):
        async def parent(self,hours): self.rec.write('x',{'i':1});self.rec.close()
        with mock.patch.object(live.paper.LiveTrader,'run',parent): await self.trader.run(1)
        meta=json.loads((self.root/'capture-runtime.json').read_text())
        self.assertTrue(meta['complete']);self.assertFalse(meta['feed_health_verified'])
    async def test_parent_failure_not_complete(self):
        async def parent(self,hours): raise RuntimeError('capture failed')
        with mock.patch.object(live.paper.LiveTrader,'run',parent):
            with self.assertRaisesRegex(RuntimeError,'capture failed'):await self.trader.run(1)
        self.assertFalse(json.loads((self.root/'capture-runtime.json').read_text())['complete'])
    async def test_watch_failure_cancels_parent(self):
        cancelled=asyncio.Event()
        async def parent(self,hours):
            try:await asyncio.sleep(100)
            finally:cancelled.set()
        async def watch():await asyncio.sleep(0);raise RuntimeError('writer failed')
        self.trader.writer_watch=watch
        with mock.patch.object(live.paper.LiveTrader,'run',parent):
            with self.assertRaisesRegex(RuntimeError,'writer failed'):await self.trader.run(1)
        self.assertTrue(cancelled.is_set())
        self.assertFalse(json.loads((self.root/'capture-runtime.json').read_text())['complete'])
    async def test_invalid_duration(self):
        with self.assertRaises(ValueError):await self.trader.run(0)


if __name__ == '__main__': unittest.main()
