"""Synthetic publication/restore contracts, including the raw checkpoint sidecar."""
import gzip,json,tempfile,unittest
from pathlib import Path
import artifact_parts as parts
import capture_checkpoint as checkpoint
import restore_published_archive as published


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        raw=self.root/'paper/raw/20261016';raw.mkdir(parents=True)
        with gzip.open(raw/'markets.jsonl.gz','wt') as f:f.write('{"synthetic":true}\n')
        self.src=self.root/'capture.tar';checkpoint.pack(self.root/'paper',self.src,'a'*40,'success')
        self.parts=self.root/'parts';m=parts.split_archive(self.src,self.parts,4096)
        self.sha=parts.digest_file(self.parts/'manifest.json');self.out=self.root/'restored.tar'
        self.idx={'schema':'research-archive-publication-v1','status':'all_parts_uploaded','run_id':'123',
            'prefix':'source-recording-123','manifest_sha256':self.sha,
            'parts':[{'name':p['name'],'artifact_name':f"source-recording-123-part-{p['index']:05d}",
                      'artifact_id':str(100+p['index'])} for p in m['parts']]}
        self.save()
    def save(self):
        (self.parts/'publish-index.json').write_text(json.dumps(self.idx))
    def restore(self):
        return published.restore(self.parts,self.parts,self.out,self.sha,'source-recording-123','123')
    def test_full_raw_handoff(self):
        result=self.restore();self.assertEqual(result['status'],'verified_byte_identical')
        self.assertEqual(self.out.with_suffix('.tar.sha256').read_text().strip(),parts.digest_file(self.src))
        checkpoint.restore(self.out,self.root/'restored-paper')
        self.assertTrue((self.root/'restored-paper/raw/20261016/markets.jsonl.gz').exists())
    def test_wrong_origin(self):
        self.idx['run_id']='other';self.save()
        with self.assertRaisesRegex(ValueError,'origin'):self.restore()
        self.assertFalse(self.out.exists())
    def test_incomplete_publication(self):
        self.idx['status']='partial';self.save()
        with self.assertRaisesRegex(ValueError,'incomplete'):self.restore()
    def test_missing_index_part(self):
        self.idx['parts'].pop();self.save()
        with self.assertRaisesRegex(ValueError,'count'):self.restore()
    def test_reused_artifact_id(self):
        self.idx['parts'][1]['artifact_id']=self.idx['parts'][0]['artifact_id'];self.save()
        with self.assertRaisesRegex(ValueError,'reused'):self.restore()
    def test_wrong_prefix(self):
        self.idx['parts'][0]['artifact_name']='other-part-00000';self.save()
        with self.assertRaisesRegex(ValueError,'identity'):self.restore()
    def test_wrong_manifest(self):
        self.idx['manifest_sha256']='0'*64;self.save()
        with self.assertRaisesRegex(ValueError,'manifest'):self.restore()
    def test_sidecar_never_overwritten(self):
        self.out.with_suffix('.tar.sha256').write_text('user-data')
        with self.assertRaisesRegex(ValueError,'exists'):self.restore()
        self.assertFalse(self.out.exists())


if __name__=='__main__':unittest.main()
