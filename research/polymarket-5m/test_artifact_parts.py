"""Synthetic transport safety tests; none is an economic backtest."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest import mock
import artifact_parts as a


class PartsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.source=self.root/'sealed.zip'
        self.source.write_bytes(bytes(range(256))*33)
        self.parts=self.root/'parts';self.out=self.root/'restored.zip'

    def split(self,size=1024):
        self.m=a.split_archive(self.source,self.parts,size)
        self.man=self.parts/'manifest.json';self.sha=a.digest_file(self.man)

    def restore(self):
        return a.assemble(self.man,self.parts,self.out,self.sha)

    def amend(self,fn):
        m=json.loads(self.man.read_text());fn(m);self.man.write_text(json.dumps(m));self.sha=a.digest_file(self.man)

    def test_roundtrip_tail(self):
        self.split();r=self.restore()
        self.assertEqual(self.source.read_bytes(),self.out.read_bytes())
        self.assertEqual(r['parts'],9);self.assertFalse(r['economic_replay_performed'])

    def test_exact_boundary(self):
        self.split(256);self.assertEqual(len(self.m['parts']),33);self.restore()

    def test_one_part(self):
        self.split(10000);self.assertEqual(len(self.m['parts']),1);self.restore()

    def test_missing_part_no_output(self):
        self.split();(self.parts/'part-00002.zip').unlink()
        with self.assertRaisesRegex(ValueError,'missing'):self.restore()
        self.assertFalse(self.out.exists());self.assertEqual(list(self.root.glob('.reassembly-*')),[])

    def test_tamper_part(self):
        self.split();p=self.parts/'part-00000.zip';b=bytearray(p.read_bytes());b[50]^=1;p.write_bytes(b)
        with self.assertRaisesRegex(ValueError,'hash mismatch'):self.restore()
        self.assertFalse(self.out.exists())

    def test_manifest_hash_required(self):
        self.split()
        with self.assertRaisesRegex(ValueError,'trusted'):a.assemble(self.man,self.parts,self.out,'')

    def test_manifest_changed(self):
        self.split();self.man.write_text(self.man.read_text()+' ')
        with self.assertRaisesRegex(ValueError,'manifest hash'):self.restore()

    def test_path_traversal(self):
        self.split();self.amend(lambda m:m['parts'][0].update(name='../secret'))
        with self.assertRaisesRegex(ValueError,'order/name'):self.restore()

    def test_reordered_parts(self):
        self.split();self.amend(lambda m:m['parts'].reverse())
        with self.assertRaisesRegex(ValueError,'order/name'):self.restore()

    def test_size_total(self):
        self.split();self.amend(lambda m:m.update(source_bytes=1))
        with self.assertRaisesRegex(ValueError,'source size'):self.restore()

    def test_wrong_whole_digest(self):
        self.split();self.amend(lambda m:m.update(source_sha256='0'*64))
        with self.assertRaisesRegex(ValueError,'source hash'):self.restore()
        self.assertFalse(self.out.exists())

    def test_no_overwrite(self):
        self.split();self.out.write_bytes(b'owner data')
        with self.assertRaisesRegex(ValueError,'refusing overwrite'):self.restore()
        self.assertEqual(self.out.read_bytes(),b'owner data')
        with self.assertRaisesRegex(ValueError,'refusing overwrite'):a.split_archive(self.source,self.parts)

    def test_symlinks(self):
        self.split();p=self.parts/'part-00000.zip';p.rename(self.root/'moved.zip');p.symlink_to(self.root/'moved.zip')
        with self.assertRaisesRegex(ValueError,'missing/invalid'):self.restore()
        s=self.root/'source-link';s.symlink_to(self.source)
        with self.assertRaisesRegex(ValueError,'symlink'):a.split_archive(s,self.root/'other')

    def test_invalid_chunk_sizes(self):
        for size in (0,-1,True,a.LIMIT,a.LIMIT-a.BLOCK+1):
            with self.assertRaises(ValueError):a.split_archive(self.source,self.parts,size)

    def test_empty(self):
        self.source.write_bytes(b'')
        with self.assertRaisesRegex(ValueError,'empty'):a.split_archive(self.source,self.parts)

    def test_zip_extra_members(self):
        self.split();p=self.parts/'part-00000.zip'
        with zipfile.ZipFile(p,'a') as z:z.writestr('../escape',b'bad')
        self.amend(lambda m:m['parts'][0].update(zip_bytes=p.stat().st_size,zip_sha256=a.digest_file(p)))
        with self.assertRaisesRegex(ValueError,'members'):self.restore()
        self.assertFalse((self.root/'escape').exists())

    def test_compressed_member_rejected(self):
        self.split();p=self.parts/'part-00000.zip'
        with zipfile.ZipFile(p,'w',compression=zipfile.ZIP_DEFLATED) as z:z.writestr('payload.bin',self.source.read_bytes()[:1024])
        # ZIP size less than payload is rejected even before decompression.
        self.amend(lambda m:m['parts'][0].update(zip_bytes=p.stat().st_size,zip_sha256=a.digest_file(p)))
        with self.assertRaisesRegex(ValueError,'size'):self.restore()

    def test_duplicate_manifest_keys(self):
        self.split();s=self.man.read_text();s=s.replace('{','{"schema":"duplicate",',1);self.man.write_text(s);self.sha=a.digest_file(self.man)
        with self.assertRaisesRegex(ValueError,'duplicate'):self.restore()

    def test_mutating_source_rejected(self):
        real=a.digest_file
        def mutate(path):
            if str(path).endswith('part-00000.zip'):self.source.write_bytes(b'changed')
            return real(path)
        with mock.patch.object(a,'digest_file',side_effect=mutate):
            with self.assertRaisesRegex(ValueError,'source'):a.split_archive(self.source,self.parts,1024)
        self.assertFalse(self.parts.exists());self.assertEqual(list(self.root.glob('.artifact-parts-*')),[])

    def test_invalid_manifest_shapes(self):
        self.split();original=self.man.read_text()
        for value in ([],None,{'schema':a.SCHEMA,'parts':[None]}):
            self.man.write_text(json.dumps(value));self.sha=a.digest_file(self.man)
            with self.assertRaises(ValueError):self.restore()
        self.man.write_text(original);self.sha=a.digest_file(self.man)
        self.amend(lambda m:m['parts'][0].update(index=False))
        with self.assertRaises(ValueError):self.restore()


if __name__=='__main__':unittest.main()
