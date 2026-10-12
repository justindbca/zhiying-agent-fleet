"""Brake (budget card per quota pool + wall table) and the caller-frozen acceptance contract
(artifacts_frozen + not_contains / json_keys / cmd_exit). In-memory Redis + fake cmux on the v1
path, plus real-Redis v2 checks (the transport new fleets must use)."""
import json
import os
import sys
import time
from pathlib import Path

import pytest

from test_fleet import THREADS, FakeMgr, FakeRedis, decision, turn, write_rollout
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_runtime_v2 import fx, receive, settle, event  # noqa: F401  (fx: real-Redis v2 fleet)
from cmux_fleet import brake, fleet

REAL_FABLE_WALL = ("⎿  You've reached your Fable limit. Run /usage-credits to continue "
                   "or switch models with /model.")
REAL_CODEX_WALL = ("■ You've hit your usage limit. To continue using Codex and get access to "
                   "GPT-5.3-Codex, start a free trial of Plus today, or try again at Sep 27th, 2026.")
TIER_MAP = Path.home() / 'Documents/projects/us-stock-option/config/cli_tier_map.yaml'
THREADS = dict(THREADS, **{'worker-2': '33333333-3333-3333-3333-333333333333'})


def make(tmp_path, spec=None, workers=('worker-1',), **cfg_extra):
    """A v1 fleet with every agent idle and (optionally) one task submitted."""
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict({'run': str(run), 'redis_socket': '/tmp/cmux-fleet-test.sock', 'python': sys.executable,
                'workers': list(workers), 'handoff_ratio': .6, 'handoff_timeout': 900, 'window': 95000,
                'codex_home': str(tmp_path / 'codex-home'), 'quota_file': str(tmp_path / 'quota.json')}, **cfg_extra)
    (run / 'config.json').write_text(json.dumps(cfg))
    sessions = tmp_path / 'sessions'
    f = fleet.Fleet(run / 'config.json', FakeRedis(), FakeMgr(), sessions=sessions)
    f.start_all()
    for name in f.names:
        write_rollout(sessions, THREADS[name], fleet.agent_cwd(cfg, name))
        f.handle(turn(name, 'READY'))
    if spec is not None:
        submit(f, spec)
    return f


def submit(f, spec, rid='t1'):
    f.handle({'kind': 'task', 'request_id': rid, 'spec': spec})
    return json.loads(f.r.get('fx:reply:' + rid))


def control(f, kind, rid, **extra):
    f.handle(dict({'kind': kind, 'request_id': rid}, **extra))
    return json.loads(f.r.get('fx:reply:' + rid))


def quota(tmp_path, **pools):
    now = time.time()
    (tmp_path / 'quota.json').write_text(json.dumps({'at': now, 'pools': {
        k: dict(v, at=now) for k, v in pools.items()}}))


# ------------------------------------------------------------------ wall table (pure)
def test_wall_table_classifies_real_wall_texts_and_ignores_work_screens():
    assert brake.classify_wall(REAL_FABLE_WALL)['scope'] == 'model_pool'
    assert brake.classify_wall(REAL_CODEX_WALL)['scope'] == 'account'
    assert brake.classify_wall("1. Set up usage credits and continue\n2. Switch model")['scope'] == 'model_pool'
    # E18: a weekly-limit screen that also offers /usage-credits is account-level, not a model pool.
    assert brake.classify_wall("You've hit your weekly limit · resets Oct 14\nRun /usage-credits")['scope'] == 'account'
    assert brake.classify_wall("You've reached your session limit")['scope'] == 'account'
    assert brake.classify_wall('Claude AI usage limit reached|1760000000')['scope'] == 'account'
    assert brake.classify_wall('HTTP 429: Too Many Requests')['scope'] == 'account'
    for clean in ('正在分析 cu 的库存与基差', 'Read 20 files, no limit issues found', '设置 position limit 为 5 手',
                  'the API rate limit is 5/s', '', None):
        assert brake.classify_wall(clean) is None


def test_wall_action_downgrades_model_pool_walls_and_stops_account_walls():
    assert brake.downgrade('claude', 'fable') == 'claude-sonnet-5'
    assert brake.downgrade('claude', 'claude-opus-5') == 'claude-sonnet-5'
    assert brake.downgrade('claude', 'claude-sonnet-5') == ''  # bottom: switch side / stop
    assert brake.downgrade('codex', '') == 'gpt-5.6-luna'  # no model named: CLI default tier
    pool = brake.classify_wall(REAL_FABLE_WALL)
    assert brake.wall_action(pool, 'claude', 'fable') == {'action': 'downgrade', 'cli': 'claude',
                                                          'from': 'fable', 'to': 'claude-sonnet-5'}
    assert brake.wall_action(pool, 'claude', 'sonnet')['action'] == 'stop'
    account = brake.wall_action(brake.classify_wall(REAL_CODEX_WALL), 'codex', 'gpt-5.6-sol')
    assert account['action'] == 'stop' and 'switch side' in account['advice']


@pytest.mark.skipif(not TIER_MAP.is_file(), reason='us-stock-option cli_tier_map.yaml not on this machine')
def test_wall_table_absorbs_cli_tier_map():
    import yaml
    src = yaml.safe_load(TIER_MAP.read_text())
    table = brake.wall_table()
    assert table['same_cli_downgrade'] == src['same_cli_downgrade']
    assert table['aliases'] == src['aliases'] and table['default'] == src['default']


# ------------------------------------------------------------------ budget card (pure)
@pytest.mark.parametrize('raw, error', [
    ({'pool': 'pool-b', 'max_usd': 10}, 'max_usd applies only to pool pool-a'),
    ({'max_usd': 10}, 'max_usd applies only to pool pool-a'),
    ({'pool': 'pool-a', 'max_week_pct': 80}, 'max_week_pct applies only to pool pool-b'),
    ({'pool': 'pool-b', 'max_week_pct': 120}, 'at most 100'),
    ({'pool': 'other'}, 'pool must be one of'),
    ({'max_calls': 0}, 'max_calls must be a positive integer'),
    ({'max_calls': True}, 'max_calls must be a positive integer'),
    ({'max_rounds': 2.5}, 'max_rounds must be a positive integer'),
    ({'max_hours': float('inf')}, 'max_hours must be a positive number'),
    ({'max_tokens': 5}, 'budget may only have'),
    ({}, 'non-empty JSON object'),
    ([1], 'non-empty JSON object')])
def test_budget_card_rejects_wrong_pool_lines_and_bad_numbers(raw, error):
    with pytest.raises(ValueError, match=error):
        brake.validate_budget(raw)


def test_budget_card_counters_and_quota_readings():
    assert brake.validate_budget(None) is None
    card = brake.validate_budget({'pool': 'pool-a', 'max_usd': 40, 'max_calls': 3})
    now = time.time()
    q = {'pools': {'pool-a': {'cash_used_usd': 141.5, 'at': now}}}
    state = {'budget': json.dumps(card), 'calls': '3', 'rounds': '1', 'started': str(now - 7200), 'usd_base': '100'}
    values = brake.counters(state, q, now)
    assert values['usd'] == 41.5 and values['calls'] == 3 and values['hours'] == 2.0
    assert [t['limit'] for t in brake.trips(card, values)] == ['max_calls', 'max_usd']
    stale = {'pools': {'pool-a': {'cash_used_usd': 999, 'at': now - 7200}}}
    assert brake.counters(state, stale, now)['usd'] is None  # unknown never trips
    assert brake.trips(card, dict(values, usd=None, calls=2)) == []
    assert brake.pool_reading({}, 'pool-b') is None
    assert brake.parse_raise(['max_calls=10'], card)['max_calls'] == 10
    with pytest.raises(ValueError, match='LIMIT=VALUE'):
        brake.parse_raise(['pool=pool-b'], card)


# ------------------------------------------------------------------ brake in the daemon (v1 path)
def test_max_calls_pauses_notifies_and_resume_needs_a_raise(tmp_path):
    marker = tmp_path / 'notified.txt'
    notify = [sys.executable, '-c', 'import sys; open(%r, "w").write(sys.argv[1])' % str(marker)]
    f = make(tmp_path, {'goal': 'write a report', 'budget': {'max_calls': 2}}, brake_notify=notify)
    assert 'Budget card' in f.r.lrange('fx:inbox:supervisor', 0, 0)[0]
    f.pump()  # call 1: supervisor
    f.handle(turn('supervisor', decision()))
    f.pump()  # call 2: worker-1
    assert f.agent('worker-1')['state'] == 'busy'
    f.tick()
    assert f.paused() and f.task()['status'] == 'running'  # paused, resumable, not finished
    assert f.r.hget('fx:service', 'reason').startswith('brake: budget line reached: max_calls 2/2')
    record = json.loads((f.run_dir / 'brake.json').read_text())
    assert record['kind'] == 'budget' and record['counters']['calls'] == 2 and record['budget'] == {'max_calls': 2}
    for _ in range(50):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.read_text().startswith('[cmux-fleet brake] budget line reached')
    f.handle(turn('worker-1', 'Done: worker-1/report.md'))
    f.pump()
    assert f.agent('supervisor')['state'] == 'idle' and f.r.llen('fx:inbox:supervisor') == 1  # nothing delivered
    answer = control(f, 'resume', 'c1')
    assert answer['status'] == 'rejected' and 'max_calls 2/2' in answer['reason'] and f.paused()
    assert control(f, 'resume', 'c2', **{'raise': ['pool=pool-b']})['status'] == 'rejected'
    assert control(f, 'resume', 'c3', **{'raise': ['max_calls=10']})['status'] == 'accepted'
    assert not f.paused() and f.brake_budget() == {'max_calls': 10}
    f.pump()
    assert f.agent('supervisor')['state'] == 'busy'


def test_max_rounds_counts_accepted_decisions_only(tmp_path):
    f = make(tmp_path, {'goal': 'write a report', 'budget': {'max_rounds': 2}})
    f.pump()
    f.handle(turn('supervisor', 'not json'))  # a rejected reply is not a round
    f.tick()
    assert not f.paused() and f.r.hget(fleet.BRAKE, 'rounds') == '0'
    f.pump()
    f.handle(turn('supervisor', decision()))
    f.tick()
    assert not f.paused()
    f.pump(); f.handle(turn('worker-1', 'Done: a'))
    f.pump(); f.handle(turn('supervisor', decision()))
    f.tick()
    assert f.paused() and 'max_rounds 2/2' in f.r.hget('fx:service', 'reason')


def test_budget_max_busy_only_tightens_concurrency(tmp_path):
    f = make(tmp_path, {'goal': 'two parts', 'budget': {'max_busy': 1}}, workers=('worker-1', 'worker-2'))
    f.pump()
    both = [{'to': 'worker-1', 'instruction': 'Write worker-1/a.md with part A'},
            {'to': 'worker-2', 'instruction': 'Write worker-2/b.md with part B'}]
    f.handle(turn('supervisor', decision(dispatch=both)))
    f.pump()
    assert [f.agent(w)['state'] for w in ('worker-1', 'worker-2')] == ['busy', 'idle']


def test_pool-a_pool_watches_equivalent_usd_and_unknown_never_trips(tmp_path):
    f = make(tmp_path, {'goal': 'x', 'budget': {'pool': 'pool-a', 'max_usd': 40}})
    f.tick()
    assert not f.paused() and f.r.hget(fleet.BRAKE, 'usd_base') == ''  # no quota.json yet: unknown
    quota(tmp_path, pool-a={'cash_used_usd': 100.0})
    f.tick()  # first known reading is the baseline
    assert not f.paused() and float(f.r.hget(fleet.BRAKE, 'usd_base')) == 100.0
    quota(tmp_path, pool-a={'cash_used_usd': 139.0})
    f.tick()
    assert not f.paused()
    quota(tmp_path, pool-a={'cash_used_usd': 140.5})
    f.tick()
    assert f.paused() and 'max_usd 40.5/40' in f.r.hget('fx:service', 'reason')


def test_pool-b_pool_watches_weekly_quota_percent(tmp_path):
    quota(tmp_path, pool-b={'week_used_pct': 70})
    f = make(tmp_path, {'goal': 'x', 'budget': {'pool': 'pool-b', 'max_week_pct': 85}})
    f.tick()
    assert not f.paused()
    quota(tmp_path, pool-b={'week_used_pct': 86})
    f.tick()
    assert f.paused() and 'max_week_pct 86.0/85' in f.r.hget('fx:service', 'reason')


def test_task_rejects_bad_budget_and_service_budget_lives_in_config(tmp_path):
    f = make(tmp_path)
    assert 'max_usd applies only' in submit(f, {'goal': 'x', 'budget': {'pool': 'pool-b', 'max_usd': 5}})['reason']
    with pytest.raises(ValueError, match='config budget is for service mode'):
        fleet.Fleet(_cfg(tmp_path, budget={'max_calls': 1}), FakeRedis(), FakeMgr())
    with pytest.raises(ValueError, match='brake_notify must be an argv list'):
        fleet.Fleet(_cfg(tmp_path, brake_notify='rm -rf /'), FakeRedis(), FakeMgr())
    with pytest.raises(ValueError, match='max_calls must be a positive integer'):
        fleet.Fleet(_cfg(tmp_path, mode='service', budget={'max_calls': -1}), FakeRedis(), FakeMgr())


def _cfg(tmp_path, **extra):
    path = tmp_path / ('cfg-' + str(len(list(tmp_path.glob('cfg-*')))) + '.json')
    path.write_text(json.dumps(dict({'run': str(tmp_path / 'run'), 'redis_socket': '/tmp/x.sock',
                                     'python': sys.executable, 'workers': ['worker-1']}, **extra)))
    return path


def test_model_pool_wall_on_a_turn_pauses_records_downgrade_and_keeps_the_message(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, worker_cli='claude', worker_model='fable')
    f.pump()
    f.handle(turn('supervisor', decision()))
    f.pump()
    sent = f.agent('worker-1')['last_msg']
    f.setf('worker-1', msg_id='m1')
    f.handle(dict(turn('worker-1', REAL_FABLE_WALL), msg_id='m1'))
    assert f.paused() and f.r.hget('fx:service', 'reason').startswith('brake: worker-1 hit a model_pool wall')
    wall = json.loads(f.agent('worker-1')['wall'])
    assert wall['action'] == 'downgrade' and wall['to'] == 'claude-sonnet-5'
    assert f.r.lrange('fx:inbox:worker-1', 0, -1) == [sent]  # redelivered after resume
    assert not f.r.llen('fx:inbox:supervisor')  # the wall text never becomes a report
    assert json.loads((f.run_dir / 'brake.json').read_text())['decision']['to'] == 'claude-sonnet-5'
    assert control(f, 'resume', 'c1')['status'] == 'accepted'  # a wall pause resumes plainly
    assert not f.paused() and f.r.hget(fleet.BRAKE, 'tripped') == ''


def test_long_report_mentioning_limits_is_not_a_wall(tmp_path):
    f = make(tmp_path, {'goal': 'x'})
    f.pump(); f.handle(turn('supervisor', decision())); f.pump()
    report = 'Done: worker-1/report.md\n' + 'The vendor said "rate limited" and 429 Too Many Requests. ' * 10
    f.handle(turn('worker-1', report))
    assert not f.paused() and '[worker-1 report]' in f.r.lrange('fx:inbox:supervisor', 0, -1)[-1]


def test_account_wall_on_a_stalled_screen_stops(tmp_path):
    f = make(tmp_path, {'goal': 'x'}, stall_timeout=60)
    f.pump(); f.handle(turn('supervisor', decision())); f.pump()
    f.mgr.capture_to = lambda pane, path: Path(path).write_text('working...\n' + REAL_CODEX_WALL + '\n> ')
    stale = time.time() - 3600
    f.setf('worker-1', busy_since=stale)
    path = write_rollout(f.sessions, THREADS['worker-1'], fleet.agent_cwd(f.cfg, 'worker-1'))
    os.utime(path, (stale, stale))
    f.check_stalls(time.time())
    assert f.paused()
    wall = json.loads(f.agent('worker-1')['wall'])
    assert wall['scope'] == 'account' and wall['action'] == 'stop'
    assert json.loads((f.run_dir / 'brake.json').read_text())['source'] == 'screen'


def test_next_task_starts_a_fresh_card_and_clears_a_brake_pause(tmp_path):
    f = make(tmp_path, {'goal': 'x', 'budget': {'max_calls': 1}})
    f.pump(); f.tick()
    assert f.paused()
    f.handle({'kind': 'stop', 'request_id': 's1'})
    f.stopping = False
    assert submit(f, {'goal': 'y'}, 't2')['status'] == 'accepted'
    assert not f.paused() and f.brake_budget() is None and f.r.hget(fleet.BRAKE, 'calls') == '0'


def test_operator_pause_is_not_cleared_by_a_new_task(tmp_path):
    f = make(tmp_path)
    control(f, 'pause', 'p1')
    submit(f, {'goal': 'y'})
    assert f.paused()


# ------------------------------------------------------------------ frozen acceptance contract
def art(path='worker-1/verdict.md', **extra):
    return dict({'path': path, 'contains': ['判决'], 'not_contains': ['TODO'],
                 'json_keys': ['verdict', 'p0']}, **extra)


@pytest.mark.parametrize('spec, error', [
    ({'goal': 'x', 'contract': {'artifacts_frozen': True}}, "non-empty artifacts"),
    ({'goal': 'x', 'contract': {'artifacts_frozen': True}, 'artifacts': []}, "non-empty artifacts"),
    ({'goal': 'x', 'contract': {'artifacts_frozen': 'yes'}, 'artifacts': [art()]}, 'artifacts_frozen'),
    ({'goal': 'x', 'contract': {'interfaces': True}, 'artifacts': [art()]}, 'artifacts_frozen'),
    ({'goal': 'x', 'contract': {'artifacts_frozen': True}, 'artifacts': [art(json_keys=[])]}, 'json_keys'),
    ({'goal': 'x', 'contract': {'artifacts_frozen': True}, 'artifacts': [art(cmd_exit='make test')]}, 'cmd_exit')])
def test_frozen_contract_spec_is_validated(tmp_path, spec, error):
    answer = submit(make(tmp_path), spec)
    assert answer['status'] == 'rejected' and error in answer['reason']


def test_frozen_artifacts_ignore_and_audit_supervisor_additions_and_use_extended_checks(tmp_path):
    f = make(tmp_path, {'goal': 'review', 'contract': {'artifacts_frozen': True}, 'artifacts': [art()]})
    assert 'frozen by the caller' in f.r.lrange('fx:inbox:supervisor', 0, 0)[0]
    out = f.workspace() / 'worker-1/verdict.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    extra = [{'path': 'worker-1/easy.md'}]
    f.pump()
    f.handle(turn('supervisor', decision(artifacts=extra)))
    assert json.loads(f.task()['artifacts']) == [art()]
    logged = [json.loads(x) for x in f.r.lrange('fx:log', 0, -1)]
    assert any(x['kind'] == 'contract.append_ignored' and x['dropped'] == extra for x in logged)
    f.pump(); f.handle(turn('worker-1', 'Done'))
    steps = [('判决：PASS\nTODO fix\n{"verdict": "PASS", "p0": 0}', 'forbidden text'),
             ('判决：PASS\n{"verdict": "PASS"}', 'missing JSON key p0'),
             ('判决：PASS, no machine line', 'no JSON object')]
    for text, why in steps:
        f.r.hset('fx:task', mapping={'bounces': 0})  # three false completions block; each step is one
        out.write_text(text)
        f.pump()
        f.handle(turn('supervisor', decision('COMPLETED', 100, [])))
        assert f.task()['status'] == 'running', text
        assert why in f.r.lrange('fx:inbox:supervisor', 0, -1)[-1]
    f.r.hset('fx:task', mapping={'bounces': 0})
    out.write_text('# 复核\n判决：PASS\n```json\n{"verdict": "PASS", "p0": 0, "p1": 0}\n```')
    f.pump()
    f.handle(turn('supervisor', decision('COMPLETED', 100, [], artifacts=extra)))
    assert f.task()['status'] == 'completed'
    f.write_result()
    result = json.loads((f.run_dir / 'result.json').read_text())
    assert [a['path'] for a in result['artifacts']] == ['worker-1/verdict.md'] and result['artifacts'][0]['problems'] == []


def test_frozen_artifact_cmd_exit_runs_the_callers_command(tmp_path):
    check = {'path': 'checks/tests.txt', 'cmd_exit': [sys.executable, '-c', 'print("7 passed")'],
             'contains': ['passed'], 'not_contains': ['failed']}
    f = make(tmp_path, {'goal': 'x', 'contract': {'artifacts_frozen': True}, 'artifacts': [check]})
    f.pump()
    f.handle(turn('supervisor', decision('COMPLETED', 100, [])))
    assert f.task()['status'] == 'completed'
    assert (f.workspace() / 'checks/tests.txt').read_text().strip() == '7 passed'


def test_unfrozen_task_keeps_old_artifact_rules(tmp_path):
    """Old tasks: supervisor additions still merge; json_keys / not_contains stay ignored."""
    f = make(tmp_path, {'goal': 'x', 'artifacts': [art()]})
    (f.workspace() / 'worker-1').mkdir(parents=True, exist_ok=True)
    (f.workspace() / 'worker-1/verdict.md').write_text('判决 TODO, no JSON')
    (f.workspace() / 'worker-1/easy.md').write_text('ok')
    f.pump()
    f.handle(turn('supervisor', decision(artifacts=[{'path': 'worker-1/easy.md'}])))
    assert len(json.loads(f.task()['artifacts'])) == 2
    f.pump(); f.handle(turn('worker-1', 'Done'))
    f.pump(); f.handle(turn('supervisor', decision('COMPLETED', 100, [])))
    assert f.task()['status'] == 'completed'


def test_json_key_failures_reads_whole_file_or_final_object():
    assert fleet.json_key_failures('{"a": 1, "b": 2}', ['a', 'b']) == []
    assert fleet.json_key_failures('text {"x": 1} more\n{"a": 1}', ['a']) == []
    assert fleet.json_key_failures('[1, 2]', ['a']) == ['no JSON object (whole file or final line)']
    assert fleet.json_key_failures('{"a": 1}\ntrailing words', ['a']) == ['no JSON object (whole file or final line)']


# ------------------------------------------------------------------ v2 transport (real Redis)
def test_v2_task_budget_pauses_in_a_transaction_and_resume_raises(fx):
    f = fx
    f.service = False; f.cfg['mode'] = 'task'
    event(f, dict(kind='task', request_id='new-task', spec={
        'goal': 'write an evidence report', 'budget': {'max_calls': 1, 'max_busy': 1}}))
    f.pump(); env = receive(f, 'supervisor')
    assert f.r.hget(fleet.BRAKE, 'calls') == '1'
    f.tick()
    assert f.paused() and f.task()['status'] == 'running'
    audit = [x['event'] for _, x in f.r.xrange(f.v2.bus.audit_key)]
    assert 'brake.tripped' in audit and (f.run_dir / 'brake.json').exists()
    settle(f, 'supervisor', env, {'status': 'RUNNING', 'completion': 20, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/report.md with Conclusion evidence.'}]})
    assert f.r.hget(fleet.BRAKE, 'rounds') == '1'
    f.pump()
    assert f.agent('worker-1')['state'] == 'idle' and f.v2.depth('worker-1') == 1  # held by the pause
    event(f, dict(kind='resume', request_id='res-1'))
    assert f.paused()
    event(f, dict(kind='resume', request_id='res-2', **{'raise': ['max_calls=5']}))
    assert not f.paused() and f.brake_budget()['max_calls'] == 5
    f.pump()
    assert f.agent('worker-1')['state'] == 'busy'


def test_start_budget_flag_is_service_only_and_validated(capsys):
    with pytest.raises(SystemExit, match='--budget is the service budget card'):
        fleet.main(['start', '--budget', '{"max_calls": 1}'])
    with pytest.raises(SystemExit, match='--budget rejected: budget max_usd applies only to pool pool-a'):
        fleet.main(['start', '--mode', 'service', '--budget', '{"max_usd": 1}'])


def test_service_budget_card_comes_from_config(tmp_path):
    f = make(tmp_path, mode='service', budget={'max_calls': 1})
    f.tick()  # first tick starts the service card
    assert f.brake_budget() == {'max_calls': 1} and not f.paused()
    f.handle({'kind': 'request', 'request_id': 'q1', 'spec': {
        'id': 'r1', 'payload': {'m': 'hi'}, 'reply': {'required': ['reply']}}})
    f.pump()
    f.tick()
    assert f.paused() and 'max_calls 1/1' in f.r.hget('fx:service', 'reason')
