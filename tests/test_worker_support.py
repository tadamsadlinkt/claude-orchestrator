import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).parents[1] / 'scripts'
spec = importlib.util.spec_from_file_location('support', SCRIPTS / 'worker_support.py')
support = importlib.util.module_from_spec(spec)
spec.loader.exec_module(support)
MODEL = 'claude-opus-5-5'


def assistant(model=MODEL, error=None):
    e = {'type': 'assistant', 'message': {'model': model, 'content': []}}
    if error:
        e['error'] = error
    return e


def result(**extra):
    return {'type': 'result', 'subtype': 'success', 'is_error': False, **extra}


class ClassificationTests(unittest.TestCase):
    def test_synthetic_failures_are_not_model_mismatches(self):
        for code, expected in [('rate_limit', 'rate_limit'), ('authentication_failed', 'authentication'), ('server_error', 'provider_error')]:
            for prefix in ([], [assistant()]):
                with self.subTest(code=code, prefix=bool(prefix)):
                    rec = support.classify(prefix + [assistant('<synthetic>', code), result()], MODEL, 0)
                    self.assertEqual(rec['failure_class'], expected)
                    self.assertEqual(rec['status'], 'failed')
                    self.assertEqual(rec['observed_models'], [MODEL] if prefix else [])
                    self.assertEqual(rec['model_identity_status'], 'matched' if prefix else 'unverified')

    def test_real_wrong_model_stays_a_failure(self):
        rec = support.classify([assistant('different-model'), assistant('<synthetic>', 'rate_limit'), result()], MODEL, 0)
        self.assertEqual(rec['failure_class'], 'model_mismatch')

    def test_completion_requires_all_evidence(self):
        for events, code, expected in [
            ([assistant(), result()], 0, None),
            ([assistant(), result(subtype='error_max_turns')], 0, 'turn_limit'),
            ([assistant(), result(permission_denials=[{'tool_name': 'Read'}])], 0, None),
            ([assistant(), result(is_error=True, permission_denials=[{'tool_name': 'Read'}])], 0, 'permission_denied'),
            ([assistant(), result(is_error=True)], 0, 'execution_error'),
            ([assistant(), result()], 2, 'execution_error'),
            ([assistant()], 0, 'missing_result'),
            ([result()], 0, 'identity_unverified'),
            ([assistant(), assistant('<synthetic>'), result()], 0, 'synthetic_error'),
        ]:
            self.assertEqual(support.classify(events, MODEL, code)['failure_class'], expected)

    def test_recovery_needs_reconciliation_and_has_a_limit(self):
        rec = dict(status='failed', account_profile=support.account_profile(), consecutive_failures=1)
        with self.assertRaises(ValueError): support.check_resume(rec, None)
        support.check_resume(rec, 'Inspected diff; narrowed remaining question')
        rec['consecutive_failures'] = 3
        with self.assertRaises(ValueError): support.check_resume(rec, 'Try again')

    def test_resume_cannot_switch_account_or_use_unknown_profile(self):
        for profile in ('/different/profile', None):
            with self.assertRaises(ValueError):
                support.check_resume(dict(status='exited', account_profile=profile), None)


class LeaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.repo = self.base / 'repo'; self.repo.mkdir()
        def git(*args):
            return subprocess.check_output(['git', '-C', str(self.repo), *args], stderr=subprocess.DEVNULL, text=True).strip()
        self.git = git
        git('init'); git('config', 'user.name', 'Test'); git('config', 'user.email', 'test@example.invalid')
        git('commit', '--allow-empty', '-m', 'fixture')
        self.home = self.base / 'lifecycle'; (self.home / 'bin').mkdir(parents=True)
        # Resolve the repository's lifecycle runtime, never the live registry.
        source = Path(__file__).resolve().parents[4] / 'scripts/dev/worktree_lifecycle.py'
        if not source.exists():
            source = Path.home() / '.adlinkt/worktree-lifecycle/bin/worktree_lifecycle.py'
        shutil.copy2(source, self.home / 'bin/worktree_lifecycle.py')
        self.patch = patch.object(support, 'LIFECYCLE_HOME', self.home); self.patch.start(); self.addCleanup(self.patch.stop)
        spec = importlib.util.spec_from_file_location('test_lifecycle', self.home / 'bin/worktree_lifecycle.py')
        mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
        state = self.home / support.hashlib.sha256(str((self.repo / '.git').resolve()).encode()).hexdigest()[:20]
        self.app = mod.Lifecycle(self.repo, state); self.app.init()
        self.path = self.base / 'worker'; self.record = self.app.create(self.path, 'parent', 'test', 'HEAD')

    def lease(self, mode='review', owner='parent', task='task'):
        return support.WorkerLease(self.path, owner, task, 'attempt', mode, 10).acquire()

    def test_hold_blocks_archive_and_is_released_after_completion(self):
        lease = self.lease()
        self.assertIn(lease.dependency, self.app.load()['records'][self.record['id']]['holds'])
        with self.assertRaisesRegex(Exception, 'dependent resources'):
            self.app.blockers(self.app.load()['records'][self.record['id']])
        lease.release()
        self.assertTrue(lease.info['hold_released'])
        self.assertEqual(self.app.load()['records'][self.record['id']]['holds'], {})

    def test_wrong_owner_and_unregistered_checkout_are_rejected(self):
        with self.assertRaises(Exception): self.lease(owner='other')
        unknown = self.base / 'legacy'; self.git('worktree', 'add', '--detach', str(unknown), 'HEAD')
        with self.assertRaisesRegex(ValueError, 'Unregistered'):
            support.WorkerLease(unknown, 'parent', 'task', 'attempt', 'review', 10).acquire()
        self.assertTrue(unknown.exists())

    def test_single_writer_and_multiple_readers(self):
        first = self.lease(); second = self.lease(task='reader2')
        with self.assertRaises(ValueError): self.lease(mode='write', task='writer')
        second.release(); first.release()
        writer = self.lease(mode='write')
        with self.assertRaises(ValueError): self.lease(task='reader')
        writer.release()

    def test_primary_read_only_and_prepared_checkout_rules(self):
        lease = support.WorkerLease(self.repo, None, 'task', 'attempt', 'review', 10)
        self.assertTrue(lease.info['primary_read_only'])
        with self.assertRaises(Exception):
            support.WorkerLease(self.repo, 'parent', 'task', 'attempt', 'write', 10).acquire()
        state = self.app.load(); state['records'][self.record['id']]['state'] = 'prepared'; self.app.save(state)
        with self.assertRaises(ValueError): self.lease()

    def test_runner_releases_real_hold_after_success_and_launch_failure(self):
        spec = importlib.util.spec_from_file_location('runner_integration', SCRIPTS / 'claude_task.py')
        runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)
        runner.ROOT = self.base / 'tasks'
        runner.CLAUDE = self.base / 'fake-claude'
        # The fake provider checks that launch happened after registration of its hold.
        registry = str(self.app.file)
        runner.CLAUDE.write_text('#!/usr/bin/env python3\nimport json\n' +
            'state=json.load(open(' + repr(registry) + '))\n' +
            'assert any(r["holds"] for r in state["records"].values())\n' +
            'print(json.dumps({"type":"assistant","message":{"model":"claude-opus-5-5","content":[]}}))\n' +
            'print(json.dumps({"type":"result","subtype":"success","is_error":False}))\n')
        runner.CLAUDE.chmod(0o700)
        prompt = self.base / 'prompt'; prompt.write_text('Offline fixture')
        argv = ['runner', 'run', '--cwd', str(self.path), '--owner', 'parent', '--model', 'opus-5.5', '--prompt-file', str(prompt)]
        with patch.object(runner.support, 'LIFECYCLE_HOME', self.home), patch.object(sys, 'argv', argv), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(), 0)
            runner.CLAUDE = self.base / 'absent-executable'
            self.assertEqual(runner.main(), 1)
        self.assertEqual(self.app.load()['records'][self.record['id']]['holds'], {})
        receipts = [json.loads(p.read_text()) for p in runner.ROOT.glob('*/task.json')]
        self.assertEqual({r['status'] for r in receipts}, {'exited', 'failed'})
        self.assertTrue(all(r['lifecycle']['hold_released'] for r in receipts))

    def test_subdirectories_resolve_ownership_without_changing_worker_cwd(self):
        primary_subdir = self.repo / 'src'; primary_subdir.mkdir()
        worker_subdir = self.path / 'src'; worker_subdir.mkdir()
        primary = support.WorkerLease(primary_subdir, None, 'task', 'attempt', 'review', 10)
        self.assertTrue(primary.info['primary_read_only'])
        for mode in ('review', 'write'):
            lease = support.WorkerLease(worker_subdir, 'parent', 'task', 'attempt', mode, 10).acquire()
            self.assertEqual(lease.info['record_id'], self.record['id'])
            lease.release()
        with self.assertRaises(Exception):
            support.WorkerLease(worker_subdir, 'other', 'task', 'attempt', 'review', 10).acquire()

    def test_acquire_and_release_wait_for_transient_registry_contention(self):
        import concurrent.futures
        import time
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            with self.app.locked():
                future = pool.submit(self.lease)
                time.sleep(.1)
                self.assertFalse(future.done())
            lease = future.result(timeout=3)
            with self.app.locked():
                future = pool.submit(lease.release)
                time.sleep(.1)
                self.assertFalse(future.done())
            future.result(timeout=3)
        self.assertTrue(lease.info['hold_released'])
        self.assertEqual(self.app.load()['records'][self.record['id']]['holds'], {})

    def test_primary_review_does_not_need_registry_lock(self):
        with self.app.locked():
            lease = support.WorkerLease(self.repo, None, 'task', 'attempt', 'review', 10).acquire()
        self.assertTrue(lease.info['primary_read_only'])

    def test_interrupt_after_hold_save_still_has_releasable_identity(self):
        lease = support.WorkerLease(self.path, 'parent', 'task', 'attempt', 'review', 10)
        save = lease.app.save
        def interrupt(value):
            save(value)
            raise KeyboardInterrupt()
        with patch.object(lease.app, 'save', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt): lease.acquire()
        self.assertIn(lease.dependency, self.app.load()['records'][self.record['id']]['holds'])
        lease.release()
        self.assertEqual(self.app.load()['records'][self.record['id']]['holds'], {})

    def test_pending_hold_must_be_reconciled_before_resume(self):
        lease = self.lease()
        rec = dict(status='exited', cleanup_status='hold_release_pending', process_cleanup_confirmed=True,
                   lifecycle=dict(lease.info), cwd=str(self.path), owner='parent', id='task', mode='review',
                   account_profile=support.account_profile(), consecutive_failures=0)
        with self.assertRaises(ValueError): support.check_resume(rec, None)
        with self.assertRaises(ValueError): support.check_resume(rec, 'Claimed cleanup without releasing hold')
        lease.release()
        support.check_resume(rec, 'Verified process exit and released the exact previous hold')
        self.assertEqual(rec['consecutive_failures'], 0)
        rec['cleanup_status'] = 'process_exit_unconfirmed'
        with self.assertRaises(ValueError): support.check_resume(rec, 'Try again')


if __name__ == '__main__':
    unittest.main()
