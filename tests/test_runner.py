import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        spec = importlib.util.spec_from_file_location('runner', Path(__file__).parents[1] / 'scripts/claude_task.py')
        self.runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.runner)
        self.runner.ROOT = self.base / 'tasks'
        self.runner.CLAUDE = self.base / 'fake-claude'
        self.runner.CLAUDE.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys, time, subprocess
pathlib.Path('invocation.json').write_text(json.dumps({'args': sys.argv[1:], 'effort_env': os.getenv('CLAUDE_CODE_EFFORT_LEVEL')}))
model = os.getenv('TEST_MODEL', sys.argv[sys.argv.index('--model') + 1])
if os.getenv('TEST_DESCENDANT'):
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    pathlib.Path('child.pid').write_text(str(child.pid))
if os.getenv('TEST_SLEEP'): time.sleep(10)
if model != 'missing':
    print(json.dumps({'type': 'assistant', 'message': {'model': model, 'content': []}}))
if os.getenv('TEST_ERROR'):
    print(json.dumps({'type': 'assistant', 'error': os.getenv('TEST_ERROR'), 'message': {'model': '<synthetic>', 'content': []}}))
print(json.dumps({'type': 'result', 'subtype': os.getenv('TEST_SUBTYPE', 'success'), 'is_error': False}))
''')
        self.runner.CLAUDE.chmod(0o700)
        self.prompt = self.base / 'prompt.txt'
        self.prompt.write_text('Probe only.')

    def invoke(self, *args):
        with patch.object(sys, 'argv', ['runner', *args]), contextlib.redirect_stdout(io.StringIO()):
            return self.runner.main()

    def start(self, *args):
        return self.invoke('run', '--cwd', str(self.base), '--model', 'opus-5.5', '--prompt-file', str(self.prompt), *args)

    def receipt(self):
        return json.loads(next(self.runner.ROOT.glob('*/task.json')).read_text())

    def test_xhigh_is_forwarded_and_preserved_on_resume(self):
        with patch.dict(os.environ, {'CLAUDE_CODE_EFFORT_LEVEL': 'low'}):
            self.assertEqual(self.start('--effort', 'xhigh'), 0)
            rec = self.receipt()
            self.assertEqual(rec['requested_effort'], 'xhigh')
            self.assertEqual(rec['effort_confirmation'], 'requested_only')
            self.assertEqual(rec['model_identity_status'], 'matched')
            self.assertEqual(self.invoke('resume', rec['id'], '--prompt-file', str(self.prompt)), 0)
        call = json.loads((self.base / 'invocation.json').read_text())
        self.assertEqual(call['args'][call['args'].index('--effort') + 1], 'xhigh')
        self.assertIsNone(call['effort_env'])
        self.assertIn('--restricted', call['args'])
        self.assertEqual(call['args'][call['args'].index('--tools') + 1], 'Read,Glob,Grep')
        self.assertEqual(self.receipt()['sandbox']['tools'], ['Read', 'Glob', 'Grep'])

    def test_fallback_is_not_success(self):
        with patch.dict(os.environ, {'TEST_MODEL': 'claude-opus-5'}):
            self.assertEqual(self.start('--effort', 'xhigh'), 1)
        self.assertEqual(self.receipt()['model_identity_status'], 'mismatch')
        self.assertEqual(self.receipt()['status'], 'failed')

    def test_missing_identity_is_not_verified(self):
        with patch.dict(os.environ, {'TEST_MODEL': 'missing'}):
            self.assertEqual(self.start(), 1)
        self.assertEqual(self.receipt()['model_identity_status'], 'unverified')

    def test_resume_rejects_effort_change(self):
        self.start('--effort', 'xhigh')
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self.invoke('resume', self.receipt()['id'], '--prompt-file', str(self.prompt), '--effort', 'low')

    def test_legacy_receipt_can_resume_without_effort(self):
        self.start()
        rec = self.receipt()
        rec.pop('requested_effort', None)
        (self.runner.ROOT / rec['id'] / 'task.json').write_text(json.dumps(rec))
        self.assertEqual(self.invoke('resume', rec['id'], '--prompt-file', str(self.prompt)), 0)
        self.assertNotIn('--effort', json.loads((self.base / 'invocation.json').read_text())['args'])

    def test_rate_failure_resume_requires_changed_condition_and_stops_after_three(self):
        with patch.dict(os.environ, {'TEST_ERROR': 'rate_limit'}):
            self.assertEqual(self.start(), 1)
            rec = self.receipt()
            self.assertEqual(rec['failure_class'], 'rate_limit')
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.invoke('resume', rec['id'], '--prompt-file', str(self.prompt))
            for _ in range(2):
                self.assertEqual(self.invoke('resume', rec['id'], '--prompt-file', str(self.prompt),
                                            '--recovery-note', 'Availability was rechecked'), 1)
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                self.invoke('resume', rec['id'], '--prompt-file', str(self.prompt), '--recovery-note', 'Again')
        self.assertEqual(self.receipt()['consecutive_failures'], 3)
        self.assertEqual(len(list(self.runner.ROOT.glob('*/*/receipt.json'))), 3)

    def test_timeout_reaps_exact_child(self):
        with patch.dict(os.environ, {'TEST_SLEEP': '1'}):
            self.assertEqual(self.start('--timeout', '1'), 124)
        self.assertEqual(self.receipt()['failure_class'], 'timeout')
        self.assertNotIn('cleanup_error', self.receipt())

    def test_result_returns_bounded_latest_text_without_reasoning(self):
        self.start()
        rec = self.receipt()
        events = self.runner.ROOT / rec['id'] / rec['attempt'] / 'events.jsonl'
        with events.open('a') as f:
            f.write(json.dumps({'type': 'assistant', 'message': {'content': [
                {'type': 'thinking', 'thinking': 'PRIVATE_REASONING'},
                {'type': 'text', 'text': 'A' * 10000 + 'LATEST_RESULT'}]}}) + '\n')
        capture = io.StringIO()
        with patch.object(sys, 'argv', ['runner', 'result', rec['id'], '--max-chars', '100']), contextlib.redirect_stdout(capture):
            self.runner.main()
        self.assertIn('LATEST_RESULT', capture.getvalue())
        self.assertNotIn('PRIVATE_REASONING', capture.getvalue())
        self.assertNotIn('A' * 101, capture.getvalue())

    def test_normal_exit_releases_owned_descendant(self):
        with patch.dict(os.environ, {'TEST_DESCENDANT': '1'}):
            self.assertEqual(self.start(), 0)
        pid = int((self.base / 'child.pid').read_text())
        with self.assertRaises(ProcessLookupError): os.kill(pid, 0)

    def test_resume_after_no_cli_launch_starts_then_resumes_conversation(self):
        executable = self.runner.CLAUDE
        self.runner.CLAUDE = self.base / 'missing-executable'
        self.assertEqual(self.start(), 1)
        rec = self.receipt()
        self.assertFalse(rec['cli_started'])
        self.runner.CLAUDE = executable
        self.assertEqual(self.invoke('resume', rec['id'], '--prompt-file', str(self.prompt),
                                     '--recovery-note', 'Executable restored; no CLI process previously launched'), 0)
        args = json.loads((self.base / 'invocation.json').read_text())['args']
        self.assertIn('--session-id', args)
        self.assertNotIn('--resume', args)
        self.assertTrue(self.receipt()['cli_started'])
        self.assertEqual(self.invoke('resume', rec['id'], '--prompt-file', str(self.prompt)), 0)
        self.assertIn('--resume', json.loads((self.base / 'invocation.json').read_text())['args'])

    def test_result_handles_preflight_failure_without_events(self):
        with patch.object(self.runner.support, 'WorkerLease', side_effect=ValueError('preflight blocked')):
            self.assertEqual(self.start(), 1)
        rec = self.receipt()
        self.assertEqual(self.invoke('result', rec['id']), 0)
        self.assertFalse(rec['cli_started'])
        self.assertNotIn('lifecycle', rec)

    def test_metadata_cleanup_failure_preserves_worker_result(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        for error in ('', 'rate_limit'):
            lease = SimpleNamespace(info={'managed': True}, acquire=lambda: None,
                                    release=Mock(side_effect=RuntimeError('registry busy')))
            with patch.dict(os.environ, {'TEST_ERROR': error}), patch.object(self.runner.support, 'WorkerLease', return_value=lease):
                self.assertEqual(self.start(), 1)
            rec = max((json.loads(p.read_text()) for p in self.runner.ROOT.glob('*/task.json')), key=lambda r: r['started'])
            self.assertEqual(rec['status'], 'failed' if error else 'exited')
            self.assertEqual(rec['failure_class'], error or None)
            self.assertEqual(rec['consecutive_failures'], 1 if error else 0)
            self.assertEqual(rec['cleanup_status'], 'hold_release_pending')
            self.assertTrue(rec['process_cleanup_confirmed'])

    def test_new_failed_attempt_does_not_reuse_previous_lifecycle(self):
        self.start(); rec = self.receipt()
        rec['lifecycle'] = {'managed': True, 'dependency': 'old-attempt', 'hold_released': True}
        (self.runner.ROOT / rec['id'] / 'task.json').write_text(json.dumps(rec))
        with patch.object(self.runner.support, 'WorkerLease', side_effect=ValueError('preflight blocked')):
            self.assertEqual(self.invoke('resume', rec['id'], '--prompt-file', str(self.prompt)), 1)
        self.assertNotIn('lifecycle', self.receipt())


if __name__ == '__main__':
    unittest.main()
