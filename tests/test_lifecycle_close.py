"""Lifecycle closing (D2/D3): done gates, auto stop + close, hold -> stop_keep_resumable, service idling.

Fake cmux and fake Pi agents on a private Redis (tests/test_runtime_v2.py fixture); merge / test /
enqueue run against a temporary git repository. No real notification leaves the machine: a notifier is
injected (and without one, no channel command is configured)."""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import event, fx, receive, req, session, settle  # noqa: F401  (fx fixture)
from cmux_fleet import fleet, lifecycle, lifecycle_close, lifecycle_deliver, lifecycle_hold, subfleet


# ------------------------------------------------------------------ fixtures
@pytest.fixture
def table(tmp_path, monkeypatch):
    """A private copy of the mode table that the daemon reads (hot reload is exercised on it)."""
    path = tmp_path / 'fleet_modes.yaml'
    shutil.copy(lifecycle.DEFAULT_PATH, path)
    monkeypatch.setattr(lifecycle, 'DEFAULT_PATH', path)
    return path


def edit_table(path, old, new):
    stat = path.stat()
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new, 1))
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))  # distinct mtime even on coarse clocks


class Sent(list):
    pass


@pytest.fixture
def notes(monkeypatch):
    out = Sent()
    out.closers = []
    monkeypatch.setattr(lifecycle_close, 'NOTIFIER', lambda rec: out.append(rec) or 'test')
    monkeypatch.setattr(lifecycle_close, 'SPAWN_CLOSER', out.closers.append)
    return out


def as_task(f, name, initiator=None):
    f.service = False
    f.cfg.update(mode='task', lifecycle=name, initiator=initiator or {'kind': 'claude', 'id': 'origin-session'})
    return f


def submit(f, spec):
    event(f, dict(kind='task', request_id='task-' + str(time.time_ns()), spec=spec))
    return f.task()


def complete(f, spec):
    """The supervisor's real COMPLETED decision through the v2 settle path."""
    submit(f, dict(spec, artifacts=[{'path': 'reviewer/verdict.json'}]))  # COMPLETED needs a verified artifact
    assert f.task()['status'] == 'running'
    f.pump(); env = receive(f, 'supervisor')
    settle(f, 'supervisor', env, {'status': 'COMPLETED', 'completion': 100, 'dispatch': [], 'reason': 'verified'})
    assert f.task()['status'] == 'completed'


def stop_tail(f):
    """The daemon loop ends (stopping set by the lifecycle); RuntimeV2.run's explicit-stop tail retires
    every runtime, writes result.json, then lifecycle_close.finalize."""
    assert f.stopping and f.explicit_stop
    f.v2.close()
    f.v2.reader = f.v2.bus.blocking_client(1000)
    f.run()


def result_json(f):
    return json.loads((f.run_dir / 'result.json').read_text())


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Temporary repo: main (deploy) checked out in a separate worktree is NOT allowed, so the repo's own
    worktree is on the feature branch and main is only a ref."""
    r = tmp_path / 'repo'
    r.mkdir()
    git(r, 'init', '-q', '-b', 'main')
    git(r, 'config', 'user.email', 't@example.org'); git(r, 'config', 'user.name', 't')
    (r / 'a.txt').write_text('base\n')
    git(r, 'add', '.'); git(r, 'commit', '-qm', 'base')
    git(r, 'checkout', '-qb', 'feat/x')
    (r / 'a.txt').write_text('feature\n')
    git(r, 'commit', '-qam', 'feature')
    return r


def deliver_spec(repo, tmp_path, test_cmd=None):
    return {'repo': str(repo), 'feature_branch': 'feat/x', 'deploy_branch': 'main',
            'test_cmd': test_cmd or [sys.executable, '-c', 'print("12 passed")'],
            'queue': str(tmp_path / 'queue' / 'deploy-queue.jsonl'), 'review_file': 'reviewer/verdict.json'}


def write_verdict(f, verdict='PASS', reviewer='opus', rnd=1, issues=(), name='reviewer/verdict.json'):
    path = f.workspace() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({'round': rnd, 'verdict': verdict, 'reviewer': reviewer, 'issues': list(issues)}))


# ------------------------------------------------------------------ dev_task: gate, auto stop, close
def test_dev_task_completed_merges_tests_enqueues_then_stops_closes_and_notifies(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    spec = deliver_spec(repo, tmp_path)
    write_verdict(f)
    complete(f, {'goal': 'ship feature x', 'deliver': spec})
    sha = git(repo, 'rev-parse', 'feat/x')
    f.tick()
    assert git(repo, 'rev-parse', 'main') == sha  # fast-forwarded by trusted Python
    queue = [json.loads(x) for x in Path(spec['queue']).read_text().splitlines()]
    assert queue == [dict(queue[0], status='pending_approval', sha=sha, deploy_branch='main', feature_branch='feat/x')]
    assert f.lifecycle_close['closed_by'] == 'auto'
    panes = [f.agent(s)['pane'] for s in f.names]
    stop_tail(f)
    assert set(panes) <= set(f.mgr.killed)  # every runtime retired before anything is closed
    out = result_json(f)
    assert out['status'] == 'completed' and out['mode'] == 'dev_task' and out['closed_by'] == 'auto'
    assert out['closed_at'] >= out['at'] and out['workspace'] == 'closing' and 'resumable' not in out
    assert out['delivery']['ok'] and out['delivery']['steps'] == ['review', 'fast_forward_check', 'tests', 'merged', 'enqueued']
    assert [n['channel'] for n in notes] == ['tg', 'parent']
    assert '已入队待批' in notes[0]['text'] and '已上线' not in notes[0]['text']
    assert notes[1]['initiator'] == {'kind': 'claude', 'id': 'origin-session'}
    assert len((f.run_dir / 'notifications.jsonl').read_text().splitlines()) == 2
    lifecycle_close.after_daemon(f)
    assert notes.closers == [str(f.path)]  # the detached workspace closer is launched after the daemon


def test_dev_task_gate_runs_once_and_is_idempotent(fx, table, notes, repo, tmp_path):
    spec = deliver_spec(repo, tmp_path)
    (tmp_path / 'ws').mkdir()
    (tmp_path / 'ws' / 'reviewer').mkdir()
    (tmp_path / 'ws' / 'reviewer' / 'verdict.json').write_text(json.dumps({'verdict': 'PASS', 'reviewer': 'opus'}))
    first = lifecycle_deliver.deliver(spec, tmp_path / 'ws')
    second = lifecycle_deliver.deliver(spec, tmp_path / 'ws')
    assert first['ok'] and second['ok']
    assert len(Path(spec['queue']).read_text().splitlines()) == 1  # queued once


@pytest.mark.parametrize('case,expect', [
    ('tests_fail', 'tests failed'),
    ('review_fail', 'review verdict is FAIL'),
    ('wrong_reviewer', 'the mode table asks for opus'),
    ('p1_left', '1 P1'),
    ('not_ff', 'not a fast-forward'),
    ('deploy_checked_out', 'is checked out in'),
    ('dirty', 'uncommitted changes'),
])
def test_dev_task_gate_refuses_and_never_moves_the_deploy_branch(tmp_path, repo, case, expect):
    spec = deliver_spec(repo, tmp_path)
    ws = tmp_path / 'ws'
    verdict = {'verdict': 'PASS', 'reviewer': 'opus', 'issues': []}
    if case == 'tests_fail':
        spec['test_cmd'] = [sys.executable, '-c', 'raise SystemExit(1)']
    elif case == 'review_fail':
        verdict['verdict'] = 'FAIL'
    elif case == 'wrong_reviewer':
        verdict['reviewer'] = 'sonnet'
    elif case == 'p1_left':
        verdict['issues'] = [{'id': 'X-1', 'severity': 'P1', 'title': 'open'}]
    elif case == 'not_ff':
        git(repo, 'checkout', '-q', 'main'); (repo / 'b.txt').write_text('x'); git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'diverge'); git(repo, 'checkout', '-q', 'feat/x')
    elif case == 'deploy_checked_out':
        git(repo, 'worktree', 'add', '-q', str(tmp_path / 'deploy-wt'), 'main')
    elif case == 'dirty':
        (repo / 'a.txt').write_text('uncommitted\n')
    (ws / 'reviewer').mkdir(parents=True)
    (ws / 'reviewer' / 'verdict.json').write_text(json.dumps(verdict))
    before = git(repo, 'rev-parse', 'main')
    rec = lifecycle_deliver.deliver(spec, ws)
    assert not rec['ok'] and expect in rec['problem']
    assert git(repo, 'rev-parse', 'main') == before and not Path(spec['queue']).exists()


def test_dev_task_failed_gate_turns_blocked_and_holds_instead_of_closing(fx, table, notes, repo, tmp_path):
    f = as_task(fx, 'dev_task')
    write_verdict(f)
    complete(f, {'goal': 'ship', 'deliver': deliver_spec(repo, tmp_path, [sys.executable, '-c', 'raise SystemExit(3)'])})
    f.tick()
    assert f.task()['status'] == 'blocked' and 'tests failed (exit 3)' in f.task()['reason']
    out = result_json(f)
    assert out['status'] == 'blocked' and 'closed_by' not in out  # result.json carries the gate verdict
    assert not f.stopping and f.lifecycle_close is None  # held: the run stays as it is
    assert [n['event'] for n in notes] == ['held', 'held'] and '保留现场 12 小时' in notes[0]['text']
    f.tick()
    assert len(notes) == 2  # the hold is announced once


def test_dev_task_submit_requires_a_deliver_spec(fx, table):
    f = as_task(fx, 'dev_task')
    event(f, dict(kind='task', request_id='no-deliver', spec={'goal': 'ship'}))
    answer = json.loads(f.r.get('fx:reply:no-deliver'))
    assert answer['status'] == 'rejected' and 'needs "deliver"' in answer['reason']


# ------------------------------------------------------------------ hold -> stop_keep_resumable -> resume
def test_blocked_hold_then_stop_keep_resumable_and_resume_reopens(fx, table, notes, monkeypatch):
    f = as_task(fx, 'program')
    submit(f, {'goal': 'phase 1', 'hours': 2})
    f.finish('blocked', 'needs a human decision')
    f.tick()
    held_at = result_json(f)['at']
    lifecycle_close.tick(f, now=held_at + 11.9 * 3600)
    assert not f.stopping  # inside hold_hours: everything stays
    assert all(p in f.mgr.alive for p in (f.agent(s)['pane'] for s in f.names))
    lifecycle_close.tick(f, now=held_at + 12 * 3600 + 1)
    assert f.lifecycle_close == dict(f.lifecycle_close, closed_by='timeout', resumable=True)
    stop_tail(f)
    out = result_json(f)
    assert out['status'] == 'blocked' and out['resumable'] is True and out['closed_by'] == 'timeout'
    assert 'start --resume' in notes[-1]['text']
    assert fleet.resume_target(str(f.run_dir)) == f.run_dir  # not "already finished"
    fields = lifecycle_hold.reopen(f.r, f.task())
    assert fields['status'] == 'running' and f.task()['status'] == 'running'


def test_reopen_of_budget_exhausted_needs_extend_hours(fx, table):
    f = as_task(fx, 'dev_task')
    task = {'id': 't1', 'status': 'budget_exhausted', 'deadline': str(time.time() - 5)}
    with pytest.raises(ValueError, match='--extend-hours'):
        lifecycle_hold.reopen(f.r, task)
    fields = lifecycle_hold.reopen(f.r, task, extend_hours=2)
    assert fields['deadline'] > time.time() + 7000


def test_explicit_stop_result_stays_not_resumable(fx, table, notes):
    f = as_task(fx, 'program')
    submit(f, {'goal': 'x'})
    event(f, {'kind': 'stop', 'request_id': 'stop-user'})
    stop_tail(f)
    out = result_json(f)
    assert out['closed_by'] == 'user' and out['status'] == 'cancelled' and 'resumable' not in out
    with pytest.raises(SystemExit, match='already finished'):
        fleet.resume_target(str(f.run_dir))


def test_hold_hours_edit_takes_effect_on_the_next_daemon_tick(fx, table, notes):
    f = as_task(fx, 'program')
    submit(f, {'goal': 'x'})
    f.finish('budget_exhausted', 'time budget exhausted')
    f.tick()
    at = result_json(f)['at']
    lifecycle_close.tick(f, now=at + 2 * 3600)
    assert not f.stopping  # 12 h hold
    edit_table(table, 'on_budget_exhausted: {hold_hours: 12', 'on_budget_exhausted: {hold_hours: 1')
    lifecycle_close.tick(f, now=at + 2 * 3600)  # same daemon, next tick, no restart
    assert f.lifecycle_close['closed_by'] == 'timeout' and f.lifecycle_close['resumable']


# ------------------------------------------------------------------ smoke / scheduled / review_loop
def test_smoke_failure_closes_at_once(fx, table, notes):
    f = as_task(fx, 'smoke')
    submit(f, {'goal': 'probe the pair'})
    f.finish('blocked', 'worker-1 startup timed out before READY')
    f.tick()
    assert f.lifecycle_close['closed_by'] == 'auto'
    panes = [f.agent(s)['pane'] for s in f.names]
    stop_tail(f)
    assert set(panes) <= set(f.mgr.killed) and not (set(panes) & f.mgr.alive)
    assert result_json(f)['closed_by'] == 'auto' and result_json(f)['status'] == 'blocked'


def test_scheduled_hard_timeout_ends_and_closes(fx, table, notes):
    f = as_task(fx, 'scheduled')
    submit(f, {'goal': 'sector rotation 09:00'})
    limit = lifecycle.mode_policy('scheduled', 'hard_timeout_min')  # the yaml's value, not a copy of it
    t0 = time.time()
    lifecycle_close.tick(f, now=t0)
    lifecycle_close.tick(f, now=t0 + (limit - 1) * 60)
    assert f.task()['status'] == 'running'
    lifecycle_close.tick(f, now=t0 + limit * 60 + 1)
    assert f.task()['status'] == 'budget_exhausted' and 'hard_timeout_min ' + str(limit) in f.task()['reason']
    assert f.lifecycle_close['closed_by'] == 'timeout'


def test_review_loop_cap_ends_with_outstanding_list(fx, table, notes):
    f = as_task(fx, 'review_loop')
    submit(f, {'goal': 'fix review findings', 'review_file': 'reviewer/verdict.json'})
    issues = [{'id': 'P1-02', 'severity': 'P1', 'title': 'race in finish()'},
              {'id': 'P2-01', 'severity': 'P2', 'title': 'naming', 'status': 'fixed'}]
    write_verdict(f, 'FAIL', rnd=2, issues=issues)
    f.tick()
    assert f.task()['status'] == 'running'  # round 2 < max_rounds 3
    write_verdict(f, 'FAIL', rnd=3, issues=issues)
    f.tick()
    assert f.task()['status'] == 'blocked' and 'max_rounds 3' in f.task()['reason']
    listing = json.loads((f.run_dir / 'outstanding.json').read_text())
    assert [i['id'] for i in listing['issues']] == ['P1-02']
    assert 'P1-02' in (f.run_dir / 'outstanding.md').read_text()
    assert f.lifecycle_close['closed_by'] == 'auto' and f.lifecycle_close['report'].endswith('outstanding.json')
    stop_tail(f)
    assert result_json(f)['outstanding'].endswith('outstanding.json')


def test_review_loop_completed_needs_a_clean_pass(fx, table, notes):
    f = as_task(fx, 'review_loop')
    write_verdict(f, 'PASS', reviewer='whoever')
    complete(f, {'goal': 'fix', 'review_file': 'reviewer/verdict.json'})
    f.tick()
    assert f.lifecycle_close['closed_by'] == 'auto' and f.task()['status'] == 'completed'


# ------------------------------------------------------------------ service: reclaim, respawn, idle stop
def test_service_idle_worker_reclaimed_supervisor_kept_and_respawned_on_demand(fx, table, notes):
    f = fx
    f.cfg['lifecycle'] = 'service'
    old = f.agent('worker-1')['pane']
    now = time.time()
    lifecycle_close.tick(f, now=now + 29 * 60)
    assert f.agent('worker-1')['state'] == 'idle'
    lifecycle_close.tick(f, now=now + 31 * 60)
    assert f.agent('worker-1')['state'] == 'reclaimed' and old in f.mgr.killed and not f.agent('worker-1')['pane']
    assert f.agent('supervisor')['pane'] in f.mgr.alive  # the supervisor always stays
    f.v2.tick()  # the liveness check must not hold the fleet for the cleared pane
    assert not f.paused()
    f.mgr.dock_control = lambda pane: True  # real CmuxSessionManager call made by respawn
    req(f, 'r9', 'worker-1')
    f.pump()
    lifecycle_close.tick(f)
    assert f.agent('worker-1')['state'] == 'starting' and f.agent('worker-1')['pane'] not in ('', old)
    assert f.agent('worker-1')['generation'] == '1'


def test_service_idle_stop_hours_null_by_default_then_hot_enabled(fx, table, notes):
    f = fx
    f.cfg['lifecycle'] = 'service'
    edit_table(table, 'worker_idle_reclaim_min: 30', 'worker_idle_reclaim_min: null')
    now = time.time()
    lifecycle_close.tick(f, now=now)
    lifecycle_close.tick(f, now=now + 100 * 3600)
    assert not f.stopping
    edit_table(table, 'idle_stop_hours: null', 'idle_stop_hours: 2')
    lifecycle_close.tick(f, now=now + 100 * 3600)
    assert f.lifecycle_close['closed_by'] == 'timeout'
    stop_tail(f)
    out = result_json(f)
    assert out['status'] == 'stopped' and 'idle_stop_hours' in out['reason'] and out['closed_by'] == 'timeout'


def test_reclaimed_slot_counts_as_retired_on_stop(fx, table, notes):
    f = fx
    f.cfg['lifecycle'] = 'service'
    lifecycle_close.tick(f, now=time.time() + 3600)
    assert f.agent('worker-1')['state'] == 'reclaimed'
    event(f, {'kind': 'stop', 'request_id': 'stop-svc'})
    stop_tail(f)
    assert result_json(f)['closed_by'] == 'user' and result_json(f)['workspace'] == 'closing'


# ------------------------------------------------------------------ scope: legacy runs, sub-fleets
def test_runs_without_lifecycle_are_untouched(fx, table, notes):
    f = fx
    f.service = False; f.cfg['mode'] = 'task'
    submit(f, {'goal': 'x'})
    f.finish('blocked', 'y')
    lifecycle_close.tick(f, now=time.time() + 100 * 3600)
    assert not f.stopping and not notes


def test_sub_fleet_is_closed_by_its_parent_not_by_itself(fx, table, notes):
    f = as_task(fx, 'dev_task')
    f.cfg['parent_config'] = '/parent/config.json'
    submit(f, {'goal': 'child work without deliver spec'})  # its parent checks the deliverable
    assert f.task()['status'] == 'running'
    f.finish('completed', 'done')
    lifecycle_close.tick(f, now=time.time() + 100 * 3600)
    assert not f.stopping and not notes


@pytest.mark.parametrize('parent,child', [('service', 'dev_task'), ('program', 'dev_task'), ('dev_task', 'dev_task'),
                                          (None, None)])
def test_child_config_lifecycle_comes_from_child_mode(parent, child, table):
    assert subfleet.child_lifecycle({'lifecycle': parent}) == child


# ------------------------------------------------------------------ workspace closer + ownership
class Registry:
    def __init__(self, meta, ok=True):
        self.meta, self.ok, self.closed = meta, ok, False
        self.meta_path = Path('/nonexistent/registry.json')

    def _meta(self):
        return self.meta

    def close_all(self):
        self.closed = True
        return self.ok

    def _surfaces(self, ws):
        return {} if self.ok or ws is None else {'s1': {}}


def test_closer_only_closes_a_registry_owned_by_this_run(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    fleet.save(run / 'config.json', {'run': str(run)})
    fleet.save(run / 'result.json', {'status': 'completed'})
    mine = Registry({'run': str(run), 'surfaces': {'s1': {'output_file': str(run / 'prompts' / 'w.log')}}})
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=mine) == 'closed' and mine.closed
    other = Registry({'run': str(tmp_path / 'other'), 'surfaces': {}})
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=other).startswith('kept: cmux registry belongs')
    assert not other.closed
    mixed = Registry({'surfaces': {'s1': {'output_file': str(tmp_path / 'other' / 'w.log')}}})
    assert 'another run' in lifecycle_close.close_workspace(run / 'config.json', mgr=mixed) and not mixed.closed
    failing = Registry({'run': str(run), 'surfaces': {}, 'workspace': 'ws-1'}, ok=False)
    lifecycle_close_wait = lifecycle_close.all_gone
    lifecycle_close.all_gone = lambda mgr, meta: lifecycle_close_wait(mgr, meta, wait=0)
    try:
        assert lifecycle_close.close_workspace(run / 'config.json', mgr=failing).startswith('close_failed')
    finally:
        lifecycle_close.all_gone = lifecycle_close_wait
    lingering = Registry({'run': str(run), 'surfaces': {}, 'workspace': 'ws-1'}, ok=False)
    lingering._surfaces = lambda ws: {}  # close_all timed out but nothing of ours is live: closed
    assert lifecycle_close.close_workspace(run / 'config.json', mgr=lingering) == 'closed'
    assert fleet.load(run / 'result.json')['workspace'] == 'closed'


def test_stop_unconfirmed_keeps_workspace_and_alarms(fx, table, notes):
    f = as_task(fx, 'smoke')
    submit(f, {'goal': 'probe'})
    f.finish('blocked', 'x')
    f.tick()
    f.mgr.kill_pane = lambda pane: False  # cmux cannot confirm the close
    stop_tail(f)
    assert not f.close_workspace_pending and notes[-1]['event'] == 'stop_unconfirmed'
    assert 'closed_by' not in result_json(f)


# ------------------------------------------------------------------ notifier channels without injection
def test_without_channel_commands_nothing_leaves_the_machine(fx, table, monkeypatch, tmp_path):
    for env in (lifecycle_close.TG_ENV, lifecycle_close.PARENT_ENV):
        monkeypatch.delenv(env, raising=False)
    f = as_task(fx, 'smoke', {'kind': 'launchd', 'id': 'com.example.sector'})
    f.cfg['cmux_state_dir'] = str(tmp_path / 'state')
    recs = lifecycle_close.notify(f, 'closed', 'hello', lifecycle.get_mode('smoke'))
    assert [r['status'] for r in recs] == ['no_channel', 'mailbox']
    box = tmp_path / 'state' / 'mailbox' / 'launchd-com.example.sector.jsonl'
    assert json.loads(box.read_text())['text'] == 'hello'


def test_channel_command_gets_the_text(fx, table, monkeypatch, tmp_path):
    out = tmp_path / 'tg.txt'
    monkeypatch.setenv(lifecycle_close.TG_ENV, sys.executable + ' -c "import sys; open(%r, \'w\').write(sys.stdin.read())"' % str(out))
    f = as_task(fx, 'smoke')
    f.cfg['cmux_state_dir'] = str(tmp_path / 'state')
    recs = lifecycle_close.notify(f, 'closed', '收尾完成', lifecycle.get_mode('smoke'))
    assert recs[0]['status'] == 'sent' and out.read_text() == '收尾完成'


@pytest.mark.parametrize('value,env,expect', [
    ('claude:sess-1', {}, {'kind': 'claude', 'id': 'sess-1'}),
    (None, {'CMUX_FLEET_INITIATOR': 'program:mayfly'}, {'kind': 'program', 'id': 'mayfly'}),
    (None, {'XPC_SERVICE_NAME': 'com.justin.sector.0900'}, {'kind': 'launchd', 'id': 'com.justin.sector.0900'}),
    (None, {'XPC_SERVICE_NAME': '0'}, None),
    (None, {'XPC_SERVICE_NAME': 'application.com.cmux.123'}, None),
])
def test_initiator_recorded_at_start(value, env, expect):
    assert lifecycle_close.initiator_for_start(value, env) == expect


def test_bad_initiator_is_refused():
    with pytest.raises(ValueError, match='KIND:ID'):
        lifecycle_close.parse_initiator('nobody')


def test_stop_keep_resumable_run_is_picked_up_again_by_resume(fx, table, notes):
    f = as_task(fx, 'dev_task')
    f.mgr.has_cli_process = lambda pane, names=(): pane in f.mgr.alive
    f.mgr.dock_control = lambda pane: True
    submit(f, {'goal': 'x', 'hours': 4, 'deliver': {
        'repo': '/r', 'feature_branch': 'f', 'deploy_branch': 'm', 'test_cmd': ['true'], 'queue': '/q',
        'review_file': 'v.json'}})
    f.finish('blocked', 'needs a human')
    f.tick()
    lifecycle_close.tick(f, now=result_json(f)['at'] + 13 * 3600)
    stop_tail(f)
    assert result_json(f)['resumable'] and not (set(f.mgr.alive) - {'history-pane'})
    # start --resume: reopen (fleet.resume does this before launching the daemon), then the daemon's resume_run
    lifecycle_hold.reopen(f.r, f.task())
    (f.run_dir / 'result.json').unlink()
    again = fleet.Fleet(f.path, f.r, f.mgr)
    again.resume_requested = True
    again.v2.resume_run()
    try:
        assert again.task()['status'] == 'running' and not again.paused()
        assert all(again.agent(s)['state'] == 'starting' and again.agent(s)['generation'] == '1' for s in again.names)
        assert again.lifecycle_close is None and not again.stopping
    finally:
        again.v2.close()


class NoShutdown:
    """The fixture's Redis must survive resume()'s shutdown on the auto fallback."""
    def __init__(self, r):
        self.r, self.shut = r, False

    def __getattr__(self, name):
        return getattr(self.r, name)

    def shutdown(self):
        self.shut = True


def test_resume_of_budget_exhausted_resumable_run(fx, table, notes, monkeypatch, tmp_path):
    from types import SimpleNamespace
    from cmux_fleet import cmux_manager
    # never reach the real cmux registry or the operator's latest.json
    launched = []
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: SimpleNamespace(
        spawn_window=lambda *a, **k: launched.append(a) or SimpleNamespace(success=True)))
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    f = as_task(fx, 'program')
    fleet.save(f.path, dict(fleet.load(f.path), mode='task', lifecycle='program'))  # what start writes
    submit(f, {'goal': 'x', 'hours': 1})
    f.finish('budget_exhausted', 'time budget exhausted')
    f.r.hset(fleet.TASK, mapping={'deadline': time.time() - 1})
    f.tick()
    lifecycle_close.tick(f, now=result_json(f)['at'] + 13 * 3600)
    stop_tail(f)
    assert result_json(f)['resumable']
    client = NoShutdown(f.r)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: client)
    args = SimpleNamespace(resume=str(f.run_dir), extend_hours=None, max_attempts=None, roster=None)
    with pytest.raises(SystemExit, match='--extend-hours'):
        fleet.resume(f.run_dir, args)
    args.resume = 'auto'
    assert fleet.resume(f.run_dir, args) is False and client.shut  # auto: a new run instead
    assert f.task()['status'] == 'budget_exhausted' and (f.run_dir / 'result.json').exists() and not launched
    args.resume, args.extend_hours = str(f.run_dir), 3
    assert fleet.resume(f.run_dir, args) is True and len(launched) == 1
    assert f.task()['status'] == 'running' and float(f.task()['deadline']) > time.time() + 3 * 3600 - 60
    assert not (f.run_dir / 'result.json').exists()
    assert fleet.load(tmp_path / 'latest.json')['run'] == str(f.run_dir)
