"""Offline workflow-structure and real-shell dispatch-path tests. No GitHub calls."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[2]
WORKFLOW=ROOT/'.github/workflows/polymarket-forward.yml'


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow=yaml.load(WORKFLOW.read_text(),Loader=yaml.BaseLoader)
        cls.record=cls.workflow['jobs']['record'];cls.analyze=cls.workflow['jobs']['analyze']
    def test_acquisition_not_locked_by_analysis(self):
        self.assertNotIn('concurrency',self.workflow)
        self.assertNotEqual(self.record['concurrency']['group'],self.analyze['concurrency']['group'])
        self.assertEqual(self.record['concurrency']['cancel-in-progress'],'false')
    def test_no_research_in_capture(self):
        scripts='\n'.join(s.get('run','') for s in self.record['steps'])
        for name in ('strategy_zoo.py','h_source_race.py','theta30.py','source_confirmation.py','recording.py'):
            self.assertNotIn(name,scripts)
        self.assertIn('record_live_buffered.py live',scripts)
    def test_raw_saved_before_dispatch(self):
        names=[s.get('name','') for s in self.record['steps']]
        self.assertLess(names.index('Checkpoint raw capture before any conversion'),names.index('Save raw handoff independently of analysis'))
        self.assertLess(names.index('Save raw handoff independently of analysis'),names.index('Start the next capture without waiting for research'))
        upload=next(s for s in self.record['steps'] if s.get('id')=='raw_upload')
        self.assertIn("steps.checkpoint.outcome == 'success'",upload['if'])
        self.assertEqual(upload['uses'],'./.github/actions/publish-research-archive')
        self.assertEqual(upload['with']['archive'],'${{ runner.temp }}/capture.tar')
        self.assertEqual(upload['with']['prefix'],'source-recording-${{ github.run_id }}')
        self.assertEqual(upload['with']['retention-days'],'1')
    def test_analysis_requires_successful_capture_and_handoff(self):
        self.assertEqual(self.analyze['needs'],'record')
        for name in ('capture_outcome','checkpoint_uploaded'):
            self.assertIn(f"needs.record.outputs.{name} == 'success'",self.analyze['if'])
        steps=self.analyze['steps']
        restore=next(i for i,s in enumerate(steps) if s.get('name')=='Verify and restore immutable raw capture')
        diagnose=next(i for i,s in enumerate(steps) if s.get('name')=='Diagnose the recording')
        self.assertLess(restore,diagnose)
    def test_exact_source_commit_and_separate_state_queue(self):
        checkout=self.analyze['steps'][0]
        self.assertEqual(checkout['with']['ref'],'${{ needs.record.outputs.source_commit }}')
        self.assertEqual(self.analyze['concurrency']['queue'],'max')
    def test_all_shell_blocks_parse(self):
        for job in (self.record,self.analyze):
            for step in job['steps']:
                if 'run' not in step:continue
                script=re.sub(r'\$\{\{.*?\}\}','checked-value',step['run'])
                r=subprocess.run(['bash','-n'],input=script,text=True,capture_output=True)
                self.assertEqual(r.returncode,0,(step.get('name'),r.stderr))
    def dispatch(self,streak,capture='success',checkpoint='success',gh_exit='0'):
        script=next(s['run'] for s in self.record['steps'] if s.get('name')=='Start the next capture without waiting for research')
        script=script.replace('${{ github.ref_name }}','test-branch')
        with tempfile.TemporaryDirectory() as t:
            path=Path(t);gh=path/'gh';gh.write_text('#!/bin/sh\necho "$*" >> "$CALLS"\nexit "$GH_EXIT"\n');gh.chmod(0o700)
            env={**os.environ,'PATH':t+os.pathsep+os.environ['PATH'],'CALLS':str(path/'calls'),
                 'GH_EXIT':gh_exit,'FAILURE_STREAK':str(streak),'CAPTURE_OUTCOME':capture,'CHECKPOINT_OUTCOME':checkpoint}
            r=subprocess.run(['bash','-e','-c','sleep() { :; };\n'+script],env=env,text=True,capture_output=True)
            calls=(path/'calls').read_text().splitlines() if (path/'calls').exists() else []
            return r,calls
    def test_success_resets_failure_count(self):
        r,calls=self.dispatch(2)
        self.assertEqual(r.returncode,0);self.assertEqual(len(calls),1)
        self.assertIn('capture_failures=0',calls[0])
    def test_capture_failure_retries_with_bound(self):
        r,calls=self.dispatch(0,capture='failure')
        self.assertEqual(r.returncode,0);self.assertIn('capture_failures=1',calls[0])
        r,calls=self.dispatch(2,capture='failure')
        self.assertNotEqual(r.returncode,0);self.assertFalse(calls)
    def test_upload_failure_also_counts(self):
        r,calls=self.dispatch(0,checkpoint='skipped')
        self.assertEqual(r.returncode,0);self.assertIn('capture_failures=1',calls[0])
    def test_dispatch_api_failure_not_success(self):
        r,calls=self.dispatch(0,gh_exit='1')
        self.assertEqual(len(calls),3);self.assertNotEqual(r.returncode,0)
    def test_invalid_retry_input_does_not_dispatch(self):
        r,calls=self.dispatch('0; echo injected')
        self.assertNotEqual(r.returncode,0);self.assertFalse(calls)


if __name__=='__main__':unittest.main()

