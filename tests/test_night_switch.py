"""Night switch (docs/design.md §20): roster `profile` column, idle per-slot swap, night on|off, brief.

Real private Redis v2 + real Claude Stop hook, fake cmux and CLIs (the hot-roster harness). The
codex+pi night pair is not admitted by today's CLI matrix; its case uses a stub in the shape of
the combination table (cli_pairs.yaml rows) that feature A will provide.
"""
import json
import os

import pytest

from test_roster import REPORT, audits, hot, write_roster  # noqa: F401  (fixture)
from test_v2_codex_claude import WORKERS, answer_turn, decision, mixed, no_provisioning, submit, typed  # noqa: F401
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import fleet, night, roster, runtime_v2

DAY_CARD, NIGHT_CARD = None, 'claude-coder'  # same CLI (official Claude), other model: today's matrix admits it


def write_profiles(f, name, profiles):
    """A roster file with one row per worker and an optional profile column."""
    path = f.run_dir / name
    rows = ['workers:'] + ['  - {name: ' + w + (', profile: ' + profiles[w] if profiles.get(w) else '') + '}'
                           for w in WORKERS]
    path.write_text('\n'.join(rows) + '\n')
    stamp = getattr(f, 'test_stamp', 1_700_000_000_000_000_000) + 1_000_000_000
    f.test_stamp = stamp
    os.utime(path, ns=(stamp, stamp))
    return path


@pytest.fixture
def nightly(hot):  # noqa: F811
    f = hot
    night_path = write_profiles(f, 'night.yaml', {w: NIGHT_CARD for w in WORKERS})
    f.cfg['roster_night'] = str(night_path)
    fleet.save(f.path, dict(fleet.load(f.path), roster_night=str(night_path)))
    return f


def ready_all(f, slots):
    for w in slots:
        f.test_threads.setdefault(w, f.test_threads[w])
        assert answer_turn(f, w, 'READY', startup=True)['status'] == 'ready'


# ------------------------------------------------------------------ roster file
def test_roster_profile_column_parses_and_is_optional(tmp_path):
    path = tmp_path / 'r.yaml'
    path.write_text('workers:\n  - {name: exit, profile: flashnext-coder}\n  - name: reentry\n')
    assert roster.load_entries(path) == (['exit', 'reentry'], {}, {'exit': 'flashnext-coder'})
    assert roster.load(path) == (['exit', 'reentry'], {})  # old two-value API unchanged


def test_night_rosters_must_name_the_same_slots(tmp_path):
    cfg = dict(fleet.DEFAULTS, transport_version=2, supervisor_cli='codex', worker_cli='claude',
               allow_development_v2=True, mode='task')
    day, other = tmp_path / 'day.yaml', tmp_path / 'night.yaml'
    day.write_text('workers:\n  - name: worker-1\n  - name: worker-2\n')
    other.write_text('workers:\n  - {name: worker-1, profile: claude-coder}\n  - {name: night-2, profile: claude-coder}\n')
    with pytest.raises(ValueError, match='same worker names'):
        night.check_rosters(cfg, day, other)
    other.write_text('workers:\n  - {name: worker-1, profile: claude-coder}\n  - {name: worker-2, profile: claude-coder}\n')
    assert night.check_rosters(cfg, day, other)['night'][2] == {'worker-1': 'claude-coder', 'worker-2': 'claude-coder'}


def test_todays_matrix_refuses_a_flashnext_night_under_codex_claude(tmp_path):
    """No loosening: codex supervisor + pi worker stays refused until the combination table admits it."""
    cfg = dict(fleet.DEFAULTS, transport_version=2, supervisor_cli='codex', worker_cli='claude',
               allow_development_v2=True, mode='task')
    day, other = tmp_path / 'day.yaml', tmp_path / 'night.yaml'
    day.write_text('workers:\n  - name: worker-1\n')
    other.write_text('workers:\n  - {name: worker-1, profile: flashnext-coder}\n')
    with pytest.raises(ValueError, match='mixed Pi/'):
        night.check_rosters(cfg, day, other)


# ------------------------------------------------------------------ start / CLI
def test_start_checks_night_roster_before_provisioning(monkeypatch, tmp_path):
    no_provisioning(monkeypatch)
    base = ['start', '--supervisor-cli', 'codex', '--worker-cli', 'claude', '--allow-development-v2']
    day = tmp_path / 'day.yaml'
    day.write_text('workers:\n  - name: exit\n')
    bad = tmp_path / 'bad.yaml'
    bad.write_text('workers:\n  - {name: other, profile: claude-coder}\n')
    with pytest.raises(SystemExit, match='needs --roster'):
        fleet.main(base + ['--workers', '1', '--night-roster', str(bad)])
    with pytest.raises(SystemExit, match='same worker names'):
        fleet.main(base + ['--roster', str(day), '--night-roster', str(bad)])
    pi = tmp_path / 'pi.yaml'
    pi.write_text('workers:\n  - {name: exit, profile: flashnext-coder}\n')
    with pytest.raises(SystemExit, match='mixed Pi/'):
        fleet.main(base + ['--roster', str(day), '--night-roster', str(pi)])


def test_night_cli_writes_the_trigger_file_only_after_validation(nightly):
    f = nightly
    cfg = fleet.load(f.path)
    assert fleet.night_cmd(cfg, 'status') == {'on': False, 'roster': cfg['roster']}
    out = fleet.night_cmd(cfg, 'on')
    assert out['on'] is True and out['brake_card'] == 'night' and out['roster'] == cfg['roster_night']
    assert json.loads((f.run_dir / 'night.json').read_text())['on'] is True
    assert not list(f.run_dir.glob('night.json.*.tmp'))
    Path_night = f.run_dir / 'night.yaml'
    Path_night.write_text('workers:\n  - {name: worker-1, profile: claude-coder}\n')  # names differ now
    with pytest.raises(SystemExit, match='refused, nothing changed'):
        fleet.night_cmd(cfg, 'off')
    assert json.loads((f.run_dir / 'night.json').read_text())['on'] is True
    with pytest.raises(SystemExit, match='no night roster'):
        fleet.night_cmd(dict(cfg, roster_night=None), 'on')


# ------------------------------------------------------------------ daemon
def test_night_on_swaps_idle_slots_waits_for_busy_and_briefs_once(nightly):
    f = nightly
    submit(f)
    assert answer_turn(f, 'supervisor', decision(['worker-1']))['status'] == 'committed'
    f.pump()
    busy_pane = f.agent('worker-1')['pane']
    old = {w: f.agent(w)['pane'] for w in WORKERS}
    spawned = len(f.mgr.spawned)
    fleet.night_cmd(fleet.load(f.path), 'on')
    f.tick()
    on, = audits(f, 'night.on')
    assert on['brake_card'] == 'night' and on['supervisor'] == 'unchanged'
    assert f.r.hgetall(night.NIGHT)['on'] == '1' and f.r.hget(night.NIGHT, 'brake_card') == 'night'
    # The busy worker finishes its turn first; every idle slot is now its next generation.
    assert f.agent('worker-1')['label'] == 'worker-1' and busy_pane in f.mgr.alive
    for w in WORKERS[1:]:
        info = f.agent(w)
        assert info['label'] == w + '.1' and info['roster_profile'] == NIGHT_CARD
        assert json.loads(info['runtime_launch'])['profile_id'] == NIGHT_CARD
        assert json.loads(info['runtime_launch'])['overrides'] == {'worker_cli': 'claude', 'worker_model': 'opus'}
        assert old[w] in f.mgr.killed and info['launch_ready'] == '1' and info['state'] == 'starting'
        assert f.r.hget(fleet.NAMES, w + '.1') == w
    assert len(f.mgr.spawned) == spawned + 5
    assert f.mgr.spawned[-1]['metadata']['profile'] == 'opus'           # the card's model, not the run default
    note = audits(f, 'roster.profile_switched')[0]['handoff']
    assert 'build MAYFLY M0 parts' in open(note).read()                  # the machine handoff skeleton
    assert json.loads(audits(f, 'roster.changed')[-1]['results'])['worker-2'].startswith('profile claude-coder')
    assert not [t for t in typed(f, 'supervisor') if '[Night switch' in t]
    # worker-1 reports, then it is swapped too.
    out = f.workspace() / 'worker-1' / 'part.md'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text('findings')
    assert answer_turn(f, 'worker-1', REPORT.format(w='worker-1'))['status'] == 'committed'
    f.tick()
    assert f.agent('worker-1')['label'] == 'worker-1.1' and busy_pane in f.mgr.killed
    f.tick()
    assert not [t for t in typed(f, 'supervisor') if '[Night switch' in t]  # not before every successor is READY
    ready_all(f, WORKERS)
    f.tick()
    # Queued for the supervisor right behind worker-1's report.
    queued = [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')]
    assert queued[0].startswith('[worker-1 report]')
    text, = [t for t in queued if t.startswith('[Night switch ' + f.task()['id'] + ']')]
    assert '- worker-1.1: claude-coder (claude)' in text and 'worker-1: Write worker-1/part.md' in text
    assert 'Dispatch only work that is still missing' in text
    f.tick()
    assert len(audits(f, 'night.brief')) == 1


def test_pending_swap_holds_delivery_until_the_new_generation_is_ready(nightly):
    f = nightly
    submit(f)
    assert answer_turn(f, 'supervisor', decision(['worker-3']))['status'] == 'committed'
    # The roster changed but the tick has not run yet: the old generation gets nothing new.
    f.cfg['worker_profiles'] = {'worker-3': NIGHT_CARD}
    sent = len(typed(f, 'worker-3'))
    f.pump()
    assert len(typed(f, 'worker-3')) == sent and f.agent('worker-3')['state'] == 'idle'
    f.profile_swap_tick()
    assert f.agent('worker-3')['label'] == 'worker-3.1'
    assert f.inbox_depth('worker-3') == 1                                # queue kept on the slot
    f.pump()
    assert not typed(f, 'worker-3') or 'Write worker-3' not in typed(f, 'worker-3')[-1]
    ready_all(f, ['worker-3'])
    f.pump()
    assert 'Write worker-3/part.md' in typed(f, 'worker-3')[-1]


def test_added_worker_starts_on_its_roster_profile_without_a_swap(hot):
    f = hot
    path = f.run_dir / 'roster.yaml'
    path.write_text('workers:\n' + ''.join('  - name: ' + w + '\n' for w in WORKERS)
                    + '  - {name: extra, profile: claude-coder}\n')
    os.utime(path, ns=(f.test_stamp + 5, f.test_stamp + 5))
    f.tick()
    info = f.agent('extra')
    assert info['label'] == 'extra' and info['roster_profile'] == 'claude-coder' and info['generation'] == '0'
    assert f.mgr.spawned[-1]['metadata']['profile'] == 'opus'
    assert fleet.load(f.path)['worker_profiles'] == {'extra': 'claude-coder'}
    f.tick()
    assert not audits(f, 'roster.profile_switched')


def test_night_off_swaps_back_to_the_run_default(nightly):
    f = nightly
    fleet.night_cmd(fleet.load(f.path), 'on')
    f.tick()
    ready_all(f, WORKERS)
    fleet.night_cmd(fleet.load(f.path), 'off')
    f.tick()
    assert audits(f, 'night.off') and f.r.hget(night.NIGHT, 'brake_card') == 'day'
    for w in WORKERS:
        info = f.agent(w)
        assert info['label'] == w + '.2' and info['runtime_launch'] == '' and info['roster_profile'] == ''
    assert f.mgr.spawned[-1]['metadata']['profile'] == f.cfg['worker_model']
    ready_all(f, WORKERS)
    f.tick()
    assert audits(f, 'night.brief_skipped')                              # no running task: nothing to brief


def test_live_profile_task_defers_the_roster_swap(nightly):
    f = nightly
    f.r.hset(fleet.TASK, mapping={'apply_profiles_live': 'true'})
    f.cfg['allow_profile_live_switch'] = True
    fleet.night_cmd(fleet.load(f.path), 'on')
    f.tick()
    assert all(f.agent(w)['label'] == w for w in WORKERS)
    assert len(audits(f, 'roster.profile_deferred')) == len(WORKERS)
    f.tick()
    assert len(audits(f, 'roster.profile_deferred')) == len(WORKERS)    # logged once per slot


def test_failed_preflight_blocks_and_keeps_the_old_runtime(nightly, monkeypatch):
    f = nightly
    submit(f)
    monkeypatch.setattr(f, 'preflight_profile', lambda launch: (_ for _ in ()).throw(ValueError('claude missing')))
    pane = f.agent('worker-1')['pane']
    fleet.night_cmd(fleet.load(f.path), 'on')
    f.tick()
    assert f.task()['status'] == 'blocked' and 'claude missing' in f.task()['reason']
    assert pane in f.mgr.alive and f.agent('worker-1')['label'] == 'worker-1'


# ------------------------------------------------------------------ codex + pi (feature A's table)
def pair_table(rows):
    """Stub of the combination-table check: (supervisor_cli, worker_cli, mode) -> status."""
    real = runtime_v2.v2_cli_problem

    def problem(cfg):
        key = (cfg.get('supervisor_cli') or 'codex', cfg.get('worker_cli') or 'codex', cfg.get('mode') or 'task')
        status = rows.get(key)
        if status == 'verified':
            return None
        return real(cfg) if status is None else 'combination ' + '+'.join(key) + ' is ' + status
    return problem


def test_flashnext_night_is_rejected_whole_until_the_pair_is_admitted(hot):
    f = hot
    path = write_profiles(f, 'roster.yaml', {'worker-2': 'flashnext-coder'})
    f.cfg['roster'] = str(path)
    f.tick()
    rejected, = audits(f, 'roster.rejected')
    assert any('worker-2: profile flashnext-coder' in e and 'mixed Pi/' in e for e in json.loads(rejected['errors']))
    assert f.agent('worker-2')['label'] == 'worker-2' and not f.cfg.get('worker_profiles')


def test_flashnext_night_swaps_to_pi_when_the_table_admits_codex_pi(hot, monkeypatch):
    f = hot
    monkeypatch.setattr(runtime_v2, 'v2_cli_problem', pair_table({('codex', 'pi', 'task'): 'verified'}))
    monkeypatch.setattr(f, 'preflight_profile', lambda launch: None)
    f.cfg['pi_home'] = str(f.run_dir / 'pi-home')
    path = write_profiles(f, 'roster.yaml', {'worker-2': 'flashnext-coder'})
    f.cfg['roster'] = str(path)
    f.tick()
    info = f.agent('worker-2')
    assert info['label'] == 'worker-2.1' and info['roster_profile'] == 'flashnext-coder'
    assert f.mgr.spawned[-1]['metadata']['cli_type'] == 'pi'
    assert f.mgr.spawned[-1]['metadata']['profile'].startswith('flashnext/')
    assert not runtime_v2.tui_slot(f.v2.slot_cfg('worker-2'), 'worker-2')   # bridge slot, not typed into
    assert runtime_v2.tui_slot(f.v2.slot_cfg('worker-1'), 'worker-1')       # the day slots stay Claude TUIs
    switched, = audits(f, 'roster.profile_switched')
    assert switched['cli'] == 'pi'
    # An 'unverified' row stays refused (smoke-only admission is feature A/D's).
    monkeypatch.setattr(runtime_v2, 'v2_cli_problem', pair_table({('codex', 'pi', 'task'): 'unverified'}))
    write_profiles(f, 'roster.yaml', {'worker-3': 'flashnext-coder', 'worker-2': 'flashnext-coder'})
    f.tick()
    assert 'is unverified' in audits(f, 'roster.rejected')[-1]['errors']
