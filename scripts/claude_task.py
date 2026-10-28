#!/usr/bin/env python3
"""Run bounded Claude CLI jobs; keep receipts outside the target repository."""
import argparse
import datetime
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import uuid
import importlib.util

_support_spec = importlib.util.spec_from_file_location('worker_support', Path(__file__).with_name('worker_support.py'))
support = importlib.util.module_from_spec(_support_spec)
_support_spec.loader.exec_module(support)

ROOT = Path.home() / '.codex' / 'claude-tasks'
CLAUDE = Path.home() / '.local' / 'bin' / 'claude'
MODELS = {'opus-5.5': 'claude-opus-5-5', 'opus-5': 'claude-opus-5', 'fable-5.1': 'claude-fable-5-1'}
EFFORTS = ['low', 'medium', 'high', 'xhigh', 'max']


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(path)


def git(cwd, *args):
    return subprocess.check_output(['git', '-C', str(cwd), *args], text=True).strip()


def task_dir(value):
    return ROOT / str(uuid.UUID(value))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    for action in ('run', 'resume'):
        s = sub.add_parser(action)
        if action == 'run':
            s.add_argument('--cwd', type=Path, required=True)
            s.add_argument('--model', choices=MODELS, required=True)
            s.add_argument('--mode', choices=['review', 'write'], default='review')
        else:
            s.add_argument('task_id')
        s.add_argument('--effort', choices=EFFORTS, help='Pin effort; resume preserves the saved value')
        s.add_argument('--prompt-file', type=Path, required=True)
        s.add_argument('--owner', default=os.environ.get('CODEX_THREAD_ID') or os.environ.get('CLAUDE_SESSION_ID'))
        s.add_argument('--recovery-note', help='Required after a failed attempt; explain reconciliation and what changed')
        s.add_argument('--max-turns', type=int, default=12)
        s.add_argument('--timeout', type=int, default=900)
        s.add_argument('--allow', action='append', default=[], help='Additional narrow tool permission, write mode only')
    for action in ('status', 'result'):
        s = sub.add_parser(action)
        s.add_argument('task_id')
        if action == 'result':
            s.add_argument('--max-chars', type=int, default=12000)
    sub.add_parser('list').add_argument('--limit', type=int, default=20)
    a = p.parse_args()
    os.umask(0o077)
    if a.action == 'list':
        if not 1 <= a.limit <= 200:
            p.error('--limit must be 1..200')
        files = sorted(ROOT.glob('*/task.json'), key=lambda f: f.stat().st_mtime, reverse=True)
        for path in files[:a.limit]:
            rec = json.loads(path.read_text())
            print(json.dumps({k: rec.get(k) for k in ('id', 'status', 'model', 'failure_class', 'cwd', 'finished')}))
        return 0
    if a.action in ('status', 'result'):
        d = task_dir(a.task_id)
        rec = json.loads((d / 'task.json').read_text())
        print(json.dumps(rec, indent=2))
        if a.action == 'result':
            if not 100 <= a.max_chars <= 100000:
                p.error('--max-chars must be 100..100000; read saved logs for larger evidence')
            # Keep the latest response text; tool payloads and reasoning stay in private logs.
            tail = ''
            for event in support.load_events(d / rec['attempt'] / 'events.jsonl'):
                if event.get('type') == 'assistant':
                    for block in event.get('message', {}).get('content', []):
                        if block.get('type') == 'text':
                            tail = (tail + '\n' + block.get('text', ''))[-a.max_chars:]
            print('Response tail (bounded; full private logs retained):' + tail)
        return 0
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    if a.max_turns < 1 or a.timeout < 1:
        p.error('Turn and time limits must be positive')
    prompt = a.prompt_file.read_text()
    if not prompt.strip():
        p.error('Prompt must not be empty')
    if a.action == 'run':
        cwd = a.cwd.expanduser().resolve(strict=True)
        if not cwd.is_dir():
            p.error('cwd must be a directory')
        if a.mode == 'write':
            gd = Path(git(cwd, 'rev-parse', '--absolute-git-dir')).resolve()
            common = Path(git(cwd, 'rev-parse', '--git-common-dir'))
            if not common.is_absolute():
                common = cwd / common
            if gd == common.resolve():
                p.error('Write mode requires a separate Git worktree; create it under repository policy first')
            if git(cwd, 'status', '--porcelain'):
                p.error('New write tasks require a clean worktree; resume the owning task for follow-ups')
        ident = str(uuid.uuid4())
        d = ROOT / ident
        d.mkdir(mode=0o700)
        rec = dict(id=ident, session_id=str(uuid.uuid4()), cwd=str(cwd), mode=a.mode,
                   model=MODELS[a.model], requested_effort=a.effort, owner=a.owner,
                   account_profile=support.account_profile(), consecutive_failures=0, cli_started=False)
    else:
        d = task_dir(a.task_id)
        rec = json.loads((d / 'task.json').read_text())
        cwd = Path(rec['cwd']).resolve(strict=True)
        try:
            support.check_resume(rec, a.recovery_note)
        except ValueError as error:
            p.error(str(error))
        if rec.get('owner') and a.owner and rec['owner'] != a.owner:
            p.error('Resume cannot change the owning chat')
        rec['owner'] = rec.get('owner') or a.owner
        saved_effort = rec.get('requested_effort')
        if saved_effort is not None and a.effort is not None and a.effort != saved_effort:
            p.error('Resume cannot change pinned effort; start a new task for different settings')
        rec['requested_effort'] = saved_effort if saved_effort is not None else a.effort
    if rec['mode'] == 'write':
        gd = Path(git(cwd, 'rev-parse', '--absolute-git-dir')).resolve()
        common = Path(git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')).resolve()
        branch = git(cwd, 'branch', '--show-current')
        if gd == common or branch in ('main', 'develop'):
            p.error('Write attempts require a linked worktree on an unprotected branch or detached HEAD')
    if rec['mode'] == 'review' and a.allow:

        p.error('Additional tool grants require write mode')
    # A per-task lock prevents simultaneous resumes of the same conversation.
    import fcntl
    lock = (d / 'lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        p.error('Task already has an active runner')
    if a.action == 'resume':
        latest = json.loads((d / 'task.json').read_text())
        if latest.get('attempt') != rec.get('attempt') or latest.get('status') != rec.get('status'):
            lock.close()
            p.error('Task changed while acquiring its lock; inspect the latest receipt')
    attempt = str(uuid.uuid4())
    ad = d / attempt
    ad.mkdir(mode=0o700)
    rules = ('You are a delegated worker supervised by Codex. Follow applicable AGENTS.md and repository policies. '
             'Work only on the assigned task. Do not deploy, push, merge, access secrets, mutate live services, '
             'or install dependencies unless the assignment explicitly authorizes that action. '
             'Return changes/findings, focused verification, and blockers. Do not spawn further agents.')
    if rec['mode'] == 'review':
        rules += ' This is read-only: inspect and report; do not change files or start services.'
    (ad / 'prompt.txt').write_text(prompt)
    tool_names = ['Read', 'Glob', 'Grep']
    permissions = tool_names.copy()
    if rec['mode'] == 'write':
        tool_names += ['Edit', 'Write']
        permissions += ['Edit', 'Write']
        if a.allow:
            if any(not rule.startswith('Bash(') or not rule.endswith(')') for rule in a.allow):
                p.error('--allow accepts scoped Bash(command) rules only')
            tool_names += ['Bash']
            permissions += a.allow
    cmd = [str(CLAUDE), '--print', '--verbose', '--output-format', 'stream-json',
           '--model', rec['model'], '--max-turns', str(a.max_turns),
           '--safe-mode', '--restricted', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
           '--permission-mode', 'dontAsk', '--permission-prompts', 'none', '--no-chrome',
           '--tools', ','.join(tool_names), '--allowedTools', ','.join(permissions),
           '--append-system-prompt', rules]
    # A preflight/launch failure may precede creation of any CLI conversation.
    cmd += ['--session-id' if a.action == 'run' or rec.get('cli_started') is False else '--resume', rec['session_id']]
    if rec.get('requested_effort') is not None:
        cmd += ['--effort', rec['requested_effort']]
    env = os.environ.copy()
    if rec.get('requested_effort') is not None:
        # The environment variable takes precedence over the CLI effort flag.
        env.pop('CLAUDE_CODE_EFFORT_LEVEL', None)
    for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_BASE_URL',
                'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY'):
        env.pop(key, None)
    for stale in ('finished', 'exit_code', 'error', 'failure_class', 'provider_errors', 'synthetic_models', 'cleanup_error', 'lifecycle', 'cleanup_status', 'runner_exit_code', 'worker_result', 'process_cleanup_confirmed', 'result_subtype', 'permission_denials'):
        rec.pop(stale, None)
    rec.update(status='running', attempt=attempt, started=datetime.datetime.now(datetime.timezone.utc).isoformat(),
               runner_pid=os.getpid(), max_turns=a.max_turns, timeout_seconds=a.timeout,
               observed_models=[], model_identity_status='unverified', recovery_note=a.recovery_note,
               account_profile=support.account_profile(),
               effort_confirmation='requested_only' if rec.get('requested_effort') else 'not_requested',
               sandbox=dict(isolation='CLI tool restrictions; not an OS sandbox', safe_mode=True,
                            restricted=True, permission_mode='dontAsk', tools=tool_names,
                            allowed_tools=permissions, mcp_servers=[], chrome=False))
    save(d / 'task.json', rec)
    print(json.dumps(rec), flush=True)
    child = None
    lease = None

    def interrupt(signum, frame):
        raise KeyboardInterrupt()

    previous_sigterm = signal.signal(signal.SIGTERM, interrupt)
    try:
        lease = support.WorkerLease(cwd, rec.get('owner'), rec['id'], attempt, rec['mode'], a.timeout)
        rec['lifecycle'] = lease.info
        lease.acquire()
        save(d / 'task.json', rec)
        with (ad / 'events.jsonl').open('w') as out, (ad / 'stderr.log').open('w') as err, (ad / 'prompt.txt').open() as inp:
            child = subprocess.Popen(cmd, stdin=inp, stdout=out, stderr=err,
                                     env=env, cwd=cwd, start_new_session=True)
            rec['cli_started'] = True
            save(d / 'task.json', rec)
            code = child.wait(timeout=a.timeout)
        rec.update(support.classify(support.load_events(ad / 'events.jsonl'), rec['model'], code))
    except (subprocess.TimeoutExpired, KeyboardInterrupt) as exc:
        rec.update(status='timed_out' if isinstance(exc, subprocess.TimeoutExpired) else 'cancelled',
                   failure_class='timeout' if isinstance(exc, subprocess.TimeoutExpired) else 'cancelled', exit_code=124)
    except Exception as exc:
        rec.update(status='failed', failure_class='launch_or_lifecycle', exit_code=1, error=str(exc))
    finally:
        rec['worker_result'] = {k: rec.get(k) for k in ('status', 'exit_code', 'failure_class')}
        rec['process_cleanup_confirmed'] = False
        try:
            rec['process_cleanup_confirmed'] = support.stop_owned_group(child)
            if not rec['process_cleanup_confirmed']:
                raise RuntimeError('Owned child group exit unconfirmed; retain the lifecycle hold for reconciliation')
            rec['cleanup_status'] = 'complete'
            if lease is not None:
                lease.release()
        except Exception as exc:
            rec['cleanup_error'] = str(exc)
            rec['runner_exit_code'] = 1
            if rec['process_cleanup_confirmed']:
                # Metadata contention must not overwrite model results or consume retry budget.
                rec['cleanup_status'] = 'hold_release_pending'
            else:
                rec.update(status='failed', exit_code=1, cleanup_status='process_exit_unconfirmed', failure_class='cleanup_unconfirmed')
        rec['consecutive_failures'] = 0 if rec['status'] == 'exited' else rec.get('consecutive_failures', 0) + 1
        rec['finished'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        save(d / 'task.json', rec)
        save(ad / 'receipt.json', rec)
        lock.close()
        signal.signal(signal.SIGTERM, previous_sigterm)
    print(json.dumps(rec), flush=True)
    return rec.get('runner_exit_code', rec['exit_code'])


if __name__ == '__main__':
    sys.exit(main())
