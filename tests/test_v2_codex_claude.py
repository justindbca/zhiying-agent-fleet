"""Transport v2 with a Codex supervisor and official Claude Code workers (task mode).

Real private Redis + the real hook entry points (Codex notify, Claude Stop); fake cmux
and fake CLIs. This proves the daemon/hook/bus wiring, not a real Codex/Claude TUI.
"""
import json
from pathlib import Path
import sys
import threading
import time
import uuid

import pytest

from test_fleet import FakeMgr, write_rollout
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import cmux_manager, fleet, runtime_v2, transport_inventory

WORKERS = ['worker-' + str(i) for i in range(1, 7)]


# ------------------------------------------------------------------ gate matrix
CLIS = ('pi', 'codex', 'claude')
# Real smokes (scripts/smoke_pair.sh, 2026-10-12) verified the codex+pi, codex+claude/service and
# Claude-supervisor rows; codex+codex/task stays unverified (smoke-only).
ALLOWED = {('pi', 'pi', 'task'), ('pi', 'pi', 'service'), ('codex', 'claude', 'task'),
           ('codex', 'pi', 'task'), ('codex', 'pi', 'service'), ('codex', 'claude', 'service'),
           ('claude', 'pi', 'task'), ('claude', 'pi', 'service'), ('claude', 'claude', 'task')}
SMOKE_ONLY = {('codex', 'codex', 'task')}
# pi supervisor + official Claude workers (2026-10-13 default): smoke-only until its real smoke flips
# the row to verified (scripts/smoke_pair.sh pi claude <mode>); the test follows the shipped status.
PI_CLAUDE = {('pi', 'claude', 'task'), ('pi', 'claude', 'service')}


@pytest.mark.parametrize('mode', ['task', 'service'])
@pytest.mark.parametrize('worker', CLIS)
@pytest.mark.parametrize('supervisor', CLIS)
def test_v2_cli_matrix_allows_only_validated_pairs_with_reason(supervisor, worker, mode):
    cfg = dict(transport_version=2, supervisor_cli=supervisor, worker_cli=worker, mode=mode,
               allow_development_v2=True)
    if (supervisor, worker, mode) in PI_CLAUDE:
        from cmux_fleet import cli_pairs
        row = cli_pairs.load_pairs().lookup(supervisor, worker, mode)
        assert row and row['status'] in ('verified', 'unverified') and not row['adapter_missing']
        if row['status'] == 'unverified':
            with pytest.raises(ValueError, match='--lifecycle smoke'):
                runtime_v2.validate_config(cfg)
            cfg = dict(cfg, lifecycle='smoke')
        else:
            assert 'smoke_pair.sh pi claude ' + mode in row['verified_by'] and 'PASS' in row['verified_by']
        assert runtime_v2.validate_config(cfg) == 2
        with pytest.raises(ValueError, match='allow-development-v2'):
            runtime_v2.validate_config(dict(cfg, allow_development_v2=False))
        return
    if (supervisor, worker, mode) in ALLOWED:
        assert runtime_v2.validate_config(cfg) == 2
        with pytest.raises(ValueError, match='allow-development-v2'):
            runtime_v2.validate_config(dict(cfg, allow_development_v2=False))
        return
    with pytest.raises(ValueError) as exc:
        runtime_v2.validate_config(cfg)
    reason = str(exc.value)
    if (supervisor, worker, mode) in SMOKE_ONLY:
        assert 'Codex workers are unvalidated' in reason and '--lifecycle smoke' in reason
        assert runtime_v2.validate_config(dict(cfg, lifecycle='smoke')) == 2
    elif supervisor == 'claude':
        assert 'no Claude supervisor adapter' in reason
    elif (supervisor, worker) == ('codex', 'codex'):
        assert 'Codex workers are unvalidated' in reason
    else:
        assert 'mixed Pi/' in reason and 'unvalidated' in reason


def no_provisioning(monkeypatch):
    def forbidden():
        pytest.fail('transport rejection must precede cmux/Redis provisioning')
    monkeypatch.setattr(cmux_manager, 'cmux_available', forbidden)


@pytest.mark.parametrize('argv,match', [
    (['--mode', 'service', '--lifecycle', 'dev_task'], 'runs on --mode task, not --mode service'),
    ([], 'allow-development-v2'),
])
def test_start_rejects_codex_claude_before_provisioning(monkeypatch, argv, match):
    no_provisioning(monkeypatch)
    extra = [] if match == 'allow-development-v2' else ['--allow-development-v2']
    with pytest.raises(SystemExit, match=match):
        fleet.main(['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude'] + argv + extra)


def test_start_admits_codex_claude_task_to_provisioning(monkeypatch):
    class Reached(Exception):
        pass

    def reached():
        raise Reached
    monkeypatch.setattr(cmux_manager, 'cmux_available', reached)
    with pytest.raises(Reached):
        fleet.main(['start', '--lifecycle', 'dev_task', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--workers', '6',
                    '--allow-development-v2'])


def test_inventory_reports_mixed_support_and_reason():
    ok = {'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'task'}
    assert transport_inventory.v2_cli_problem(ok) is None
    assert transport_inventory.v2_cli_problem(dict(ok, mode='service')) is None   # verified by real smoke
    assert 'Codex workers are unvalidated' in transport_inventory.v2_cli_problem(dict(ok, worker_cli='codex'))
    assert transport_inventory.v2_cli_problem(dict(ok, worker_cli='codex', lifecycle='smoke')) is None


# ------------------------------------------------------------------ live wiring
@pytest.fixture
def mixed(private_redis_v2, tmp_path):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    sessions = tmp_path / 'codex-home' / 'sessions'
    # A real start records the resolved claude binary (cfg['claude']); the profile preflight checks
    # it. A stub that is never run keeps these tests off the host's PATH (no claude installed: fine).
    claude_bin = tmp_path / 'bin' / 'claude'
    claude_bin.parent.mkdir()
    claude_bin.write_text('#!/bin/sh\nexit 1\n')
    claude_bin.chmod(0o755)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'],
               python=sys.executable, workers=list(WORKERS), mode='task', supervisor_cli='codex',
               worker_cli='claude', claude=str(claude_bin), codex_home=str(tmp_path / 'codex-home'), transport_version=2,
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0)
    path = run / 'config.json'
    fleet.save(path, cfg)
    claude_home = tmp_path / 'claude-projects'
    f = fleet.Fleet(path, r, FakeMgr(), sessions=sessions)
    f.test_sessions, f.test_claude = sessions, claude_home
    f.test_threads = {'supervisor': str(uuid.uuid4())}
    f.test_threads.update({w: str(uuid.uuid4()) for w in WORKERS})
    import cmux_fleet.fleet as fleet_mod
    real_trust = fleet_mod.trust_claude_dir
    fleet_mod.trust_claude_dir = lambda cwd: None  # never touch the operator's ~/.claude.json
    try:
        f.start_all()
        r.set(f.v2.prefix + ':boot', 'running')
        for slot in f.names:
            assert answer_turn(f, slot, 'READY', startup=True)['status'] == 'ready'
        yield f
    finally:
        fleet_mod.trust_claude_dir = real_trust
        f.v2.close()


def drive(f, fn):
    """Run a hook (it blocks on the daemon reply) while the daemon steps."""
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault('value', fn()))
    thread.start()
    deadline = time.time() + 15
    while thread.is_alive() and time.time() < deadline:
        f.v2.step(20)
    thread.join(2)
    assert not thread.is_alive()
    return box['value']


def typed(f, slot):
    pane = f.agent(slot)['pane']
    return [text for p, text in f.mgr.sent if p == pane]


def answer_turn(f, slot, text, *, startup=False, prompt=None, generation=None):
    """Fire the real hook entry point of this slot's CLI for a finished turn."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    thread = f.test_threads[slot] if gen == 0 else f.test_threads[slot] + '-g' + str(gen)
    prompt = prompt if prompt is not None else (typed(f, slot)[-1] if typed(f, slot) and not startup else 'startup prompt')
    if slot == 'supervisor':
        turn_id = 't-' + uuid.uuid4().hex
        write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, slot), rows=[
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn_id, 'last_agent_message': text}}])
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn_id,
                              'input-messages': [prompt], 'last-assistant-message': text})
        return drive(f, lambda: fleet.notify(f.path, slot, str(gen), payload, f.test_sessions))
    transcript = f.test_claude / slot / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': prompt}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(f, lambda: fleet.claude_stop(f.path, slot, str(gen), raw))


def decision(workers):
    return json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split the work',
                       'dispatch': [{'to': w, 'instruction': 'Write ' + w + '/part.md with the findings.'}
                                    for w in workers]})


def submit(f):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': 'build MAYFLY M0 parts'}), 'new-task')
    assert f.v2.step(10)
    f.pump()


def no_message_files(f):
    return not list(f.run_dir.rglob('.inbox')) and not list(f.run_dir.rglob('msg-*.md'))


def test_startup_ready_binds_sessions_via_hooks(mixed):
    f = mixed
    for slot in f.names:
        info = f.agent(slot)
        assert info['state'] == 'idle' and info['runtime_ready'] == '1'
        assert f.v2.bus.slot_state(slot)['session_id'] == f.test_threads[slot]
    assert f.agent('supervisor')['transcript'] == ''          # Codex usage stays rollout-based
    assert f.agent('worker-1')['transcript'].endswith('.jsonl')
    sup_role = (fleet.agent_cwd(f.cfg, 'supervisor') / 'AGENTS.md').read_text()
    worker_role = (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text()
    assert 'fleet_decision' not in sup_role and 'fleet_result' not in worker_role  # original AGENTS.md formats


def test_codex_supervisor_dispatches_six_claude_workers_through_v2_bus(mixed):
    f = mixed
    submit(f)
    body, = typed(f, 'supervisor')
    lease = f.v2.bus.slot_state('supervisor')
    assert lease['status'] == 'received' and lease['consumer'] == 'tui-supervisor-g0'
    assert 'build MAYFLY M0 parts' in body and 'Reply with the JSON format from AGENTS.md.' in body
    for transport_id in (lease['message_id'], lease['current_delivery'], 'fleet_decision', '.inbox'):
        assert transport_id not in body
    assert answer_turn(f, 'supervisor', decision(WORKERS))['status'] == 'committed'
    f.pump()  # max_busy=0: all six idle workers receive in one pass
    for slot in WORKERS:
        text, = typed(f, slot)
        assert 'Write ' + slot + '/part.md' in text and 'fleet_result' not in text
        assert f.agent(slot)['state'] == 'busy'
        assert f.v2.bus.slot_state(slot)['status'] == 'received'
    assert sum(f.agent(w)['state'] == 'busy' for w in WORKERS) == 6
    # Claude needs a bracketed paste for multi-line text; Codex must not get one.
    modes = dict(f.mgr.bracketed)
    assert modes[f.agent('supervisor')['pane']] is False
    assert all(modes[f.agent(w)['pane']] is True for w in WORKERS)
    assert no_message_files(f)


def test_claude_stop_reply_settles_into_v2_and_reports_to_supervisor(mixed):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()
    out = f.workspace() / 'worker-1' / 'part.md'
    out.parent.mkdir(parents=True, exist_ok=True); out.write_text('findings')
    report = 'Done:\n- wrote part.md\nEvidence:\n- worker-1/part.md 8 bytes\nNot done: none\nBlocked: none'
    env = f.v2.bus.slot_state('worker-1')
    assert answer_turn(f, 'worker-1', report)['status'] == 'committed'
    msg = f.v2.bus.message(env['message_id'])
    assert msg['status'] == 'committed' and msg['outcome'] == 'completed'
    assert msg['result']['done'] == ['wrote part.md'] and msg['result']['evidence'] == ['worker-1/part.md 8 bytes']
    assert f.v2.bus.slot_state('worker-1')['pel_count'] == 0 and f.agent('worker-1')['state'] == 'idle'
    f.pump()
    followup = typed(f, 'supervisor')[-1]
    assert followup.startswith('[worker-1 report]') and 'wrote part.md' in followup
    assert no_message_files(f)


def test_duplicate_manual_and_old_generation_turns_are_rejected(mixed):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()
    report = 'Done:\n- x\nEvidence:\n- worker-1/x.md\nNot done: none\nBlocked: none'
    # A manual turn the operator typed in the worker tab cannot settle the delivery.
    assert answer_turn(f, 'worker-1', report, prompt='just chatting')['status'] == 'ignored'
    assert f.v2.bus.slot_state('worker-1')['status'] == 'received'
    # A retired generation's late hook is stale.
    assert answer_turn(f, 'worker-1', report, generation=5)['status'] == 'stale'
    assert answer_turn(f, 'worker-1', report)['status'] == 'committed'
    # The same Stop fired twice: nothing is received any more.
    assert answer_turn(f, 'worker-1', report, prompt=typed(f, 'worker-1')[-1])['status'] == 'ignored'
    assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 1  # exactly one report follow-up


def dispatch_one(f):
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()


def test_unparseable_worker_reply_gets_corrections_then_blocks(mixed):
    f = mixed
    dispatch_one(f)
    for attempt in range(1, runtime_v2.TUI_REPORT_RETRIES + 1):
        env = f.v2.bus.slot_state('worker-1')
        answer_turn(f, 'worker-1', '## Done\nI wrote it. Anything else?')
        assert f.v2.bus.message(env['message_id'])['outcome'] == 'failed'  # never a fake success
        assert f.task()['status'] == 'running' and not f.paused()
        assert f.agent('worker-2')['state'] == 'idle'
        f.pump()  # correction goes through the normal delivery path: new delivery + fence
        again = f.v2.bus.slot_state('worker-1')
        assert again['status'] == 'received' and again['current_delivery'] != env['current_delivery']
        assert int(again['fence']) > int(env['fence']) and again['generation'] == env['generation']
        assert 'Done: / Evidence: / Not done: / Blocked:' in typed(f, 'worker-1')[-1]
        assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 0  # no bogus report
    answer_turn(f, 'worker-1', 'Still prose.')
    assert f.task()['status'] == 'blocked'


def test_corrected_worker_report_settles_and_resets_retries(mixed):
    f = mixed
    dispatch_one(f)
    answer_turn(f, 'worker-1', 'I finished, it is in part.md.')
    f.pump()
    report = 'Done:\n- wrote part.md\nEvidence:\n- worker-1/part.md\nNot done: none\nBlocked: none'
    assert answer_turn(f, 'worker-1', report)['status'] == 'committed'
    assert f.task()['status'] == 'running' and f.agent('worker-1')['report_retries'] == '0'
    f.pump()
    assert typed(f, 'supervisor')[-1].startswith('[worker-1 report]')


def test_max_busy_caps_only_when_set(mixed):
    f = mixed
    f.cfg['max_busy'] = 2
    submit(f)
    answer_turn(f, 'supervisor', decision(WORKERS))
    f.pump()
    assert sum(bool(typed(f, w)) for w in WORKERS) == 2


def test_failed_typing_holds_slot_without_success(mixed):
    f = mixed
    f.mgr.alive.discard(f.agent('supervisor')['pane'])
    submit(f)
    assert f.agent('supervisor')['state'] == 'held' and f.paused()
    assert f.v2.bus.slot_state('supervisor')['status'] == 'uncertain'


def test_startup_must_be_ready(private_redis_v2, tmp_path):  # noqa: F811
    r = private_redis_v2
    r.flushdb()
    run = tmp_path / 'run'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=['worker-1'], mode='task', supervisor_cli='codex', worker_cli='claude',
               codex_home=str(tmp_path / 'ch'), transport_version=2, allow_development_v2=True, stall_timeout=0)
    fleet.save(run / 'config.json', cfg)
    f = fleet.Fleet(run / 'config.json', r, FakeMgr(), sessions=tmp_path / 'ch' / 'sessions')
    import cmux_fleet.fleet as fleet_mod
    real, fleet_mod.trust_claude_dir = fleet_mod.trust_claude_dir, (lambda cwd: None)
    try:
        f.start_all()
        f.test_sessions, f.test_claude = tmp_path / 'ch' / 'sessions', tmp_path / 'cp'
        f.test_threads = {'supervisor': str(uuid.uuid4()), 'worker-1': str(uuid.uuid4())}
        assert answer_turn(f, 'worker-1', 'Hello, how can I help?', startup=True)['status'] == 'rejected'
        assert f.agent('worker-1')['state'] == 'starting'  # one correction first (acceptance 10/13 P1-3)
        assert answer_turn(f, 'worker-1', 'Hello again', startup=True)['status'] == 'rejected'
        assert f.agent('worker-1')['state'] == 'dead'
    finally:
        fleet_mod.trust_claude_dir = real
        f.v2.close()


def test_renewal_extends_received_tui_lease(mixed):
    f = mixed
    submit(f)
    before = int(f.v2.bus.slot_state('supervisor')['lease_until'])
    f.v2.renewed.clear()
    time.sleep(.01)
    f.v2.renew_tui()
    assert int(f.v2.bus.slot_state('supervisor')['lease_until']) >= before
    assert any(fields.get('event') == 'renewed' for _, fields in f.r.xrange(f.v2.bus.audit_key))


def test_v2_claude_stop_hook_outlives_ready_wait(tmp_path):
    cfg = {'python': 'py', 'run': str(tmp_path), 'workers': ['worker-1'], 'transport_version': 2,
           'profile_ready_timeout': 180}
    fleet.agent_cwd(cfg, 'worker-1').mkdir(parents=True)
    argv = fleet.claude_argv('cfg.json', cfg, 'worker-1', 0, 'hi')
    settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
    assert settings['hooks']['Stop'][0]['hooks'][0]['timeout'] == 210
    del cfg['transport_version']
    argv = fleet.claude_argv('cfg.json', cfg, 'worker-1', 1, 'hi')
    settings = json.loads(Path(argv[argv.index('--settings') + 1]).read_text())
    assert settings['hooks']['Stop'][0]['hooks'][0]['timeout'] == 30  # v1 unchanged


def test_codex_turn_matches_rollout_cwd_through_symlinked_run_dir(tmp_path):
    real = tmp_path / 'real'
    (real / 'agents' / 'supervisor').mkdir(parents=True)
    link = tmp_path / 'link'
    link.symlink_to(real)
    thread = str(uuid.uuid4())
    sessions = tmp_path / 'sessions'
    write_rollout(sessions, thread, real / 'agents' / 'supervisor', rows=[
        {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': 'u1', 'last_agent_message': 'READY'}}])
    data = {'thread-id': thread, 'turn-id': 'u1', 'last-assistant-message': 'READY'}
    assert fleet.completed_turn(fleet.agent_cwd({'run': str(link)}, 'supervisor'), data, sessions)


def test_claude_settlement_survives_megabytes_of_tool_output(mixed):
    f = mixed
    submit(f)
    answer_turn(f, 'supervisor', decision(['worker-1']))
    f.pump()
    prompt = typed(f, 'worker-1')[-1]
    thread = f.test_threads['worker-1']
    transcript = f.test_claude / 'worker-1' / (thread + '.jsonl')
    report = 'Done:\n- big\nEvidence:\n- worker-1/big.log\nNot done: none\nBlocked: none'
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': prompt}},
            {'type': 'user', 'message': {'role': 'user', 'content': [
                {'type': 'tool_result', 'content': 'x' * 2_000_000}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': report}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': report})
    assert drive(f, lambda: fleet.claude_stop(f.path, 'worker-1', '0', raw))['status'] == 'committed'


def test_ignored_turn_is_audited(mixed):
    f = mixed
    answer_turn(f, 'worker-1', 'hello', prompt='manual chat')
    events = [fields for _, fields in f.r.xrange(f.v2.bus.audit_key)]
    assert any(e.get('event') == 'turn.ignored' and e.get('slot') == 'worker-1' for e in events)
