"""A slot's launch record (runtime_launch + installed_domains) carries across generations: a handoff
successor keeps model, --effort and domains whether the advisor is on or off; only a new advisor sheet
changes them. The advisor process is always a fake runner."""
import json
import os
from pathlib import Path

import pytest

from test_advisor import Runner, afx, item, report, send_decision, v2_advised, worker_text  # noqa: F401
from test_advisor_hotload import set_roster
from test_fleet import turn
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import fleet

SHEET = {'model': 'opus', 'effort': 'high', 'domains': ['research_web']}


def assert_inherited(f, generation):
    """Generation `generation` of worker-1 runs opus/high with research_web, laid out advised."""
    info = f.agent('worker-1')
    assert info['generation'] == str(generation)
    assert json.loads(info['installed_domains']) == ['research_web']
    snap = fleet.load(f.run_dir / 'runtime' / ('worker-1-g' + str(generation) + '.json'))
    argv = fleet.claude_argv(f.path, snap, 'worker-1', generation, 'READY?')
    assert argv[argv.index('--model') + 1] == 'opus' and argv[argv.index('--effort') + 1] == 'high'
    assert argv[argv.index('--setting-sources') + 1] == 'project' and '--strict-mcp-config' in argv
    cwd = fleet.agent_cwd(f.cfg, 'worker-1')
    assert 'RULES OF research_web' in (cwd / 'CLAUDE.md').read_text()
    assert sorted(p.name for p in (cwd / '.claude' / 'skills').iterdir()) == ['web-fetch-fallback']


def v1_handoff(f, generation):
    """A context-full handoff (the same start_rotation the ratio check calls), draft written by the model."""
    f.start_rotation('worker-1', 90000, 100000)
    info = f.agent('worker-1')
    draft = Path(info['handoff_draft'])
    draft.write_text('## Done (facts)\n- notes\n## In progress\nnone\n## Next steps (in order)\n1. go on\n')
    os.utime(draft, ns=(int(info['handoff_mtime']) + 10**9,) * 2)
    f.handle(dict(turn('worker-1', 'handoff written', generation), msg_id=info['msg_id']))


@pytest.mark.parametrize('online', [False, True])
def test_v1_handoff_keeps_the_sheet_config_on_or_off(afx, online):
    f = afx
    f.advisor_runner = Runner(dict(SHEET))
    send_decision(f, [item()])
    f.pump()  # fingerprint change: Phase 5 respawn to worker-1.1 with the sheet
    f.handle(turn('worker-1', 'READY', 1))
    f.pump()
    report(f, 'worker-1', 1)
    assert_inherited(f, 1)
    if not online:
        assert f.set_advisor(False, 'user', write=True) is None and not f.advisor_online()
    calls = len(f.advisor_runner.calls)
    v1_handoff(f, 1)
    assert f.agent('worker-1')['label'] == 'worker-1.2'
    assert_inherited(f, 2)
    assert len(f.advisor_runner.calls) == calls  # no new sheet: nothing re-decided


def test_v1_never_advised_slot_hands_off_plain(afx):
    """The boundary: a slot whose record is the slot defaults stays plain after the advisor goes off."""
    f = afx
    f.setf('worker-1', installed_domains='')  # e.g. released with the defaults (release_default)
    assert f.set_advisor(False, 'user', write=True) is None
    send_decision(f, [item()])
    f.pump()
    report(f, 'worker-1')
    v1_handoff(f, 0)
    snap_path = f.run_dir / 'runtime' / 'worker-1-g1.json'
    assert not snap_path.exists()  # runs off the main config, like a fleet that never had the advisor
    argv = fleet.claude_argv(f.path, fleet.load(f.path), 'worker-1', 1, 'READY?')
    assert '--setting-sources' not in argv and '--effort' not in argv


@pytest.fixture
def v2adv(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    yield from v2_advised(private_redis_v2, tmp_path, monkeypatch, ['worker-1'])


def v2_sheet_launched(f):
    from test_v2_codex_claude import answer_turn, submit
    f.advisor_runner = Runner(dict(SHEET))
    submit(f)
    answer_turn(f, 'supervisor', json.dumps({'status': 'RUNNING', 'completion': 5, 'reason': 'go', 'dispatch': [
        {'to': 'worker-1', 'id': 'M0', 'instruction': 'Research the semis rotation into worker-1/notes.md'}]}))
    f.pump()  # the held first generation is released with the sheet
    answer_turn(f, 'worker-1', 'READY', startup=True)
    f.pump()
    answer_turn(f, 'worker-1', 'Done:\n- notes\nEvidence:\n- worker-1/notes.md\nNot done: none\nBlocked: none')
    f.pump()
    assert_inherited(f, 0)


@pytest.mark.parametrize('online', [False, True])
def test_v2_handoff_keeps_the_sheet_config_on_or_off(v2adv, online):
    from test_v2_codex_claude import answer_turn
    f = v2adv
    v2_sheet_launched(f)
    if not online:
        set_roster(f, False)
        f.tick()
        assert not f.advisor_online()
    calls = len(f.advisor_runner.calls)
    f.start_rotation('worker-1', 90000, 100000)
    f.pump()
    assert answer_turn(f, 'worker-1', 'handoff written')['status'] == 'committed'
    f.v2.step(10)
    assert f.agent('worker-1')['label'] == 'worker-1.1'
    assert_inherited(f, 1)
    assert len(f.advisor_runner.calls) == calls


def test_v2_resume_mid_rescue_respawn_starts_the_successor_with_the_sheet(v2adv):
    """start --resume while an advisor respawn was still handing off: rescue_launch is not lost, and the
    relay note / hint do not leak into a later rotation. The resumed daemon starts with the advisor off."""
    from cmux_fleet import profile_launch
    f = v2adv
    v2_sheet_launched(f)
    launch = json.loads(f.agent('worker-1')['runtime_launch'])
    launch['overrides']['worker_model'], launch['extras'] = 'sonnet', {'effort': 'max'}
    f.setf('worker-1', handoff_id='h-cut', rescue_launch=profile_launch.encode_launch(launch), rescue_relay=1,
           rescue_hint='check the links', advice_domains=json.dumps(['research_web', 'us_options']))
    set_roster(f, False)
    f.v2.close()
    f.mgr.dock_control = lambda pane: None  # the control tab re-docks after a resume
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=f.test_sessions)
    assert not again.advisor_online()
    again.resume_requested = True
    again.v2.resume_run()
    info = again.agent('worker-1')
    assert info['generation'] == '1' and not info.get('rescue_launch') and not info.get('rescue_relay')
    assert not info.get('rescue_hint') and json.loads(info['installed_domains']) == ['research_web', 'us_options']
    snap = fleet.load(again.run_dir / 'runtime' / 'worker-1-g1.json')
    argv = fleet.claude_argv(again.path, snap, 'worker-1', 1, 'READY?')
    assert argv[argv.index('--model') + 1] == 'sonnet' and argv[argv.index('--effort') + 1] == 'max'
    assert argv[argv.index('--setting-sources') + 1] == 'project'
    again.v2.close()
