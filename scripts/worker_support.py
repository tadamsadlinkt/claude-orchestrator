"""Deterministic receipt classification and prospective worktree ownership."""
import datetime as dt
import contextlib
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import time

LOCK_WAIT_SECONDS = 5.0

LIFECYCLE_HOME = Path.home() / '.adlinkt/worktree-lifecycle'


def classify(events, requested_model, exit_code):
    result = None
    observed, synthetic, provider_errors = set(), set(), set()
    for e in events:
        if e.get('type') == 'result':
            result = e
            continue
        if e.get('type') != 'assistant':
            continue
        model = (e.get('message') or {}).get('model')
        if isinstance(model, str) and model:
            (synthetic if model.startswith('<') else observed).add(model)
        if e.get('error'):
            provider_errors.add(str(e['error']))
    identity = 'unverified' if not observed else ('matched' if observed == {requested_model} else 'mismatch')
    subtype = result.get('subtype') if result else None
    # Actual model substitutions remain a distinct hard failure, even with an error.
    if identity == 'mismatch':
        failure = 'model_mismatch'
    elif 'authentication_failed' in provider_errors:
        failure = 'authentication'
    elif 'rate_limit' in provider_errors:
        failure = 'rate_limit'
    elif provider_errors:
        failure = 'provider_error'
    elif synthetic:
        failure = 'synthetic_error'
    elif subtype == 'error_max_turns':
        failure = 'turn_limit'
    elif result and result.get('permission_denials') and (result.get('is_error') or subtype != 'success'):
        failure = 'permission_denied'
    elif identity == 'unverified':
        failure = 'identity_unverified'
    elif result is None:
        failure = 'missing_result'
    elif exit_code != 0 or result.get('is_error') or subtype != 'success':
        failure = 'execution_error'
    else:
        failure = None
    return dict(status='failed' if failure else 'exited',
                exit_code=exit_code or (1 if failure else 0),
                observed_models=sorted(observed), synthetic_models=sorted(synthetic),
                provider_errors=sorted(provider_errors), model_identity_status=identity,
                failure_class=failure, result_subtype=subtype,
                permission_denials=result.get('permission_denials', []) if result else [])


def account_profile():
    raw = os.environ.get('CLAUDE_CONFIG_DIR')
    return str(Path(raw).expanduser().resolve()) if raw else 'default'


def check_resume(rec, recovery_note):
    if rec['status'] == 'running':
        raise ValueError('Task is running; inspect its exact process and receipt before recovery')
    if rec.get('account_profile') != account_profile():
        raise ValueError('Account profile is different or unknown; reconcile work and start a new task. Do not resume across accounts')
    cleanup = rec.get('cleanup_status')
    if cleanup == 'process_exit_unconfirmed':
        raise ValueError('Prior process exit is unconfirmed; reconcile owned resources before starting a new task')
    if cleanup == 'hold_release_pending':
        if not recovery_note or not recovery_note.strip():
            raise ValueError('Pending hold cleanup requires --recovery-note and reconciliation before resume')
        if not rec.get('process_cleanup_confirmed'):
            raise ValueError('Prior process exit is unconfirmed')
        binding = rec.get('lifecycle') or {}
        probe = WorkerLease(Path(rec['cwd']), rec.get('owner'), rec['id'], 'reconcile', rec['mode'], 1)
        if probe.app is None or str(probe.app.state) != binding.get('state'):
            raise ValueError('Prior lifecycle identity is unavailable; reconcile before starting a new task')
        record = probe.app.owned(probe.app.load(), binding['record_id'], rec.get('owner'))
        if not binding.get('dependency') or binding['dependency'] in record['holds']:
            raise ValueError('Previous worker hold is still recorded; release that exact confirmed-exited dependency before resume')
    if rec['status'] != 'exited':
        if not recovery_note or not recovery_note.strip():
            raise ValueError('Recovery requires --recovery-note describing the reconciled work and changed condition')
        if rec.get('consecutive_failures', 0) >= 3:
            raise ValueError('Three consecutive failures: narrow/replan the assignment before starting a new task; no blind retry')


def load_events(path):
    if not path.exists():
        return
    with path.open() as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                yield value


def stop_owned_group(child):
    """Release only the process group created for this exact child, including descendants."""
    if child is None:
        return True
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            child.wait(timeout=5)
            return True
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.killpg(child.pid, 0)
        except ProcessLookupError:
            child.wait(timeout=1)
            return True
        time.sleep(.05)
    return False


def retry_registry(operation):
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            return operation()
        except Exception as exc:
            if str(exc) != 'another lifecycle operation is in progress' or time.monotonic() >= deadline:
                raise
            time.sleep(.05)


class WorkerLease:
    """No automatic adoption or archival. A hold is released only after confirmed exit."""
    def __init__(self, cwd, owner, task_id, attempt, mode, timeout):
        self.app = None
        self.info = {'managed': False}
        probe = subprocess.run(['git', '-C', str(cwd), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
                               capture_output=True, text=True)
        if probe.returncode:
            return  # Read-only non-Git assignments remain supported.
        common = str(Path(probe.stdout.strip()).resolve())
        state = LIFECYCLE_HOME / hashlib.sha256(common.encode()).hexdigest()[:20]
        if not (state / 'registry.json').exists():
            return
        runtime = LIFECYCLE_HOME / 'bin/worktree_lifecycle.py'
        spec = importlib.util.spec_from_file_location('worker_worktree_lifecycle', runtime)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.app = module.Lifecycle(cwd, state)
        self.owner = owner
        self.mode = mode
        self.timeout = timeout
        self.task_id = task_id
        self.dependency = 'worker:claude:' + task_id + ':' + attempt
        # Pure primary-checkout review does not mutate the registry or need its lock.
        if self.app.repo == self.app.primary and mode == 'review':
            self.app = None
            self.info = {'managed': False, 'primary_read_only': True}

    def acquire(self):
        if self.app is not None:
            retry_registry(self._acquire)
        return self

    def _acquire(self):
        with self.app.locked():
            value = self.app.load()
            if not self.owner:
                raise ValueError('Managed worker checkout requires --owner (the parent chat ID)')
            matches = [r for r in value['records'].values() if r['path'] == str(self.app.repo)]
            if len(matches) != 1:
                raise ValueError('Unregistered worktree: preserve it; create a new lifecycle-owned checkout')
            record = self.app.owned(value, matches[0]['id'], self.owner)
            self.app.verify_path(record)
            if record['state'] not in ('active', 'retained'):
                raise ValueError('Worktree is not active or retained; finish its current lifecycle operation first')
            for dependency, hold in record['holds'].items():
                if dependency.startswith('worker:') and (self.mode == 'write' or hold.get('workerMode') != 'review'):
                    raise ValueError('Another worker holds this checkout; one writer per worktree')
            deadline = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self.timeout + 120)
            record['holds'][self.dependency] = dict(reason='Claude ' + self.mode + ' worker ' + self.task_id,
                                                  reviewAt=deadline.isoformat(), workerMode=self.mode)
            self.record_id = record['id']
            # Publish the exact dependency to the caller before any persistent mutation.
            self.info.update(managed=True, owner=self.owner, record_id=self.record_id,
                             dependency=self.dependency, state=str(self.app.state), hold_released=False,
                             closeout='Parent must verify results, then archive or record retention')
            self.app.save(value)

    def release(self):
        if self.app is not None and self.info.get('managed'):
            retry_registry(self._release)
            self.info['hold_released'] = True

    def _release(self):
        with self.app.locked():
            value = self.app.load()
            record = self.app.owned(value, self.record_id, self.owner)
            # Interrupted acquisition can leave either a saved hold or no hold at all.
            if self.dependency in record['holds']:
                del record['holds'][self.dependency]
                self.app.save(value)
