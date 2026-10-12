"""Watchdog five states for transport v2 (docs/design.md §20).

Real private Redis + real hook entry points (the `mixed` Codex supervisor + Claude workers fleet);
fake cmux screens / ps. Time is passed explicitly to Watchdog.tick, never slept.
"""
import json
import os
import time

import pytest

from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_v2_codex_claude import answer_turn, dispatch_one, mixed, typed  # noqa: F401  (fixture)
from cmux_fleet import runtime_v2, subfleet, watchdog

DIALOG_RM = ('Bash command\n  rm -rf "$OUT/"*\n\nDangerous rm on possibly-empty variable\n'
             'Do you want to proceed?\n❯ 1. Yes\n  2. No, and tell Claude what to do differently (esc)')
DIALOG_UNKNOWN = ('Bash command\n  git push --force origin main\n\nDo you want to proceed?\n❯ 1. Yes\n  2. No')
DIALOG_RM_LITERAL = ('Bash command\n  rm -rf build/tmp\n\nDangerous rm on possibly-empty variable\n'
                     'Do you want to proceed?\n❯ 1. Yes\n  2. No')
TRUST = 'Do you trust the files in this folder?\n\n{}\n\n❯ 1. Yes, proceed\n  2. No, exit'
DIALOG_TRUST = TRUST.format('/tmp/run/workspace/worker-1')
# Claude Code 2.1.280 trust dialog, captured verbatim from a live PTY (120x40) on 2026-10-12. The path
# wraps at the dialog width; "No, exit" is preselected.
CLAUDE_TRUST_LIVE = "u\n────────────────────────────────────────────────────────────────────────────────\n Accessing workspace:\n\n /private/tmp/claude-501/-Users-justin--cmux-fleet-dev-runs-fleet-20261013-0252\n 33-a4957e88-workspace-worker-6/24b6f3ae-4cf5-4090-92f6-7f39a32c99d9/scratchpad\n /trust-probe\n\n Quick safety check: Is this a project you created or one you trust? (Like your\n own code, a well-known open source project, or work from your team). If not,\n take a moment to review what's in this folder first.\n\n Claude Code'll be able to read, edit, and execute files here.\n\n Security guide\n\n ❯ No, exit\n   Yes, I trust this folder\n\n Enter to confirm · Esc to cancel\n"
CLAUDE_TRUST_LIVE_CWD = ('/private/tmp/claude-501/-Users-justin--cmux-fleet-dev-runs-fleet-20261013-025233-a4957e88-'
                         'workspace-worker-6/24b6f3ae-4cf5-4090-92f6-7f39a32c99d9/scratchpad/trust-probe')


def claude_trust(path, width=79):
    """The live layout above for another directory, wrapped the same way."""
    rows = [' ' + path[i:i + width - 1] for i in range(0, len(path), width - 1)]
    head, tail = CLAUDE_TRUST_LIVE.split(' /private/tmp/claude-501/', 1)
    return head + '\n'.join(rows).lstrip('\n')[0:] + tail[tail.index('\n\n'):]


READ = 'Read({})\n\nRead outside the working directories?\n❯ 1. Yes\n  2. No'
WORKING = '⏺ Reading files…\n\n> '


@pytest.fixture
def wd(mixed):  # noqa: F811
    f = mixed
    f.cfg.update(stall_timeout=600, stall_grace=300, stall_escapes=3, watch_interval=0)
    f.mgr.screens, f.mgr.stats = {}, {}
    f.mgr.capture_output = lambda pane, lines=2000: f.mgr.screens.get(pane, WORKING)
    f.mgr.process_states = lambda pane: f.mgr.stats.get(pane, ['Ss', 'S+'])
    f.mgr.owns = lambda pane: pane in f.mgr.alive  # this fleet's cmux registry
    return f


def cwd(f, slot):
    from cmux_fleet.fleet import agent_cwd
    return agent_cwd(f.cfg, slot)


def audits(f, event=None):
    rows = [fields for _, fields in f.r.xrange(f.v2.bus.audit_key)]
    return [r for r in rows if event is None or r.get('event') == event]


def transitions(f, slot):
    return [(json.loads(r['detail'])['from'], json.loads(r['detail'])['to'])
            for r in audits(f, 'agent.state') if r.get('slot') == slot]


def keys(f, slot):
    pane = f.agent(slot)['pane']
    return [k for p, k in f.mgr.keys if p == pane]


def screens(f):
    folder = f.run_dir / 'watchdog'
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# ------------------------------------------------------------------ pure parts
BASE = dict(active=True, in_flight=True, age=0, stall_timeout=600, prev='working', since=0, now=1000,
            grace=300, escapes=0, max_escapes=3, screen_same=False, proc_frozen=0)


@pytest.mark.parametrize('change,state', [
    (dict(active=False, in_flight=False, age=10 ** 6), 'idle'),           # E1: idle is never dead
    ({}, 'working'),
    (dict(age=599), 'working'),
    (dict(age=600), 'stalled'),
    (dict(stall_timeout=0, age=10 ** 6), 'working'),                      # stall ladder off
    (dict(prev='stalled', since=900, age=700), 'stalled'),
    (dict(prev='stalled', since=900, moved=True), 'working'),
    (dict(prev='stalled', since=700, age=900), 'frozen'),                 # grace over
    (dict(prev='stalled', since=900, escapes=3, screen_same=True), 'frozen'),
    (dict(prev='stalled', since=900, escapes=3, screen_same=False), 'stalled'),
    (dict(proc_frozen=1), 'working'),
    (dict(proc_frozen=2), 'frozen'),                                      # ps 'U' twice
    (dict(prompt={'name': 'x'}, prompt_hits=0), 'prompting'),
    (dict(prompt={'name': 'x'}, prompt_hits=3), 'stalled'),               # same dialog 3x
    (dict(unknown='do you want to proceed?'), 'prompting'),
    (dict(in_flight=False), 'working'),                                   # starting, READY deadline rules
    (dict(in_flight=False, prompt={'name': 'trust'}), 'prompting'),
])
def test_classify_five_states(change, state):
    assert watchdog.classify(**dict(BASE, **change)) == state
    assert state in watchdog.STATES


def test_default_prompt_table_matches_the_three_known_dialogs_only():
    table = watchdog.parse_table(watchdog.DEFAULT_TABLE.read_text())
    assert [p['name'] for p in table['prompts']] == ['dangerous_rm', 'read_outside', 'trust_folder', 'trust_folder_legacy']
    assert watchdog.match_prompt(table, DIALOG_RM)[0]['name'] == 'dangerous_rm'
    assert watchdog.match_prompt(table, 'Read file\n\nRead outside the working directories?\n❯ 1. Yes')[0]['name'] == 'read_outside'
    assert watchdog.match_prompt(table, DIALOG_TRUST)[0]['name'] == 'trust_folder_legacy'
    assert watchdog.match_prompt(table, 'Trust this folder?\n› 1. Yes')[0]['name'] == 'trust_folder_legacy'
    live = watchdog.match_prompt(table, CLAUDE_TRUST_LIVE)[0]
    assert live['name'] == 'trust_folder' and live['keys'] == ['Down', 'Enter']   # Enter alone = "No, exit"
    # Cursor already on Yes (after Down): neither entry claims it, nothing is pressed again.
    moved = CLAUDE_TRUST_LIVE.replace(' ❯ No, exit\n   Yes, I trust', '   No, exit\n ❯ Yes, I trust')
    assert moved != CLAUDE_TRUST_LIVE and watchdog.match_prompt(table, moved) == (None, None)
    assert watchdog.match_prompt(table, DIALOG_UNKNOWN) == (None, 'do you want to proceed?')
    assert watchdog.match_prompt(table, WORKING) == (None, None)
    # Only the bottom of the screen counts: an answered dialog in scrollback is not a dialog.
    assert watchdog.match_prompt(table, DIALOG_RM + '\n' + '\n'.join(['output'] * 40)) == (None, None)


@pytest.mark.parametrize('bad,match', [
    ('prompts: [', 'not valid YAML'),
    ({'prompts': [{'name': 'x', 'match': ['a'], 'keys': ['rm -rf /']}]}, 'keys must be'),
    ({'prompts': [{'name': 'x', 'match': [], 'keys': ['Enter']}]}, 'match must not be empty'),
    ({'prompts': [{'name': 'x', 'match': ['a'], 'keys': ['Enter'], 'regex': '.*'}]}, 'exactly name'),
    ({'promts': []}, 'unknown prompt table keys'),
    ({'max_hits': 0}, 'max_hits'),
    ({'prompts': [{'name': 'x', 'match': ['a'], 'keys': ['Enter']}]}, 'verify must be'),   # text alone never authorises
    ({'prompts': [{'name': 'x', 'match': ['a'], 'keys': ['Enter'], 'verify': 'always'}]}, 'verify must be'),
])
def test_prompt_table_validation(bad, match):
    with pytest.raises(ValueError, match=match):
        watchdog.parse_table(bad)


def test_v2_config_validation_rejects_bad_watchdog_settings(tmp_path):
    cfg = dict(transport_version=2, supervisor_cli='codex', worker_cli='claude', mode='task', allow_development_v2=True)
    assert runtime_v2.validate_config(cfg) == 2
    with pytest.raises(ValueError, match='stall_grace'):
        runtime_v2.validate_config(dict(cfg, stall_grace=-1))
    bad = tmp_path / 'table.yaml'
    bad.write_text('prompts: [{name: x, match: [a], keys: [Delete], verify: own_cwd}]')
    with pytest.raises(ValueError, match='keys must be'):
        runtime_v2.validate_config(dict(cfg, prompt_table=str(bad)))


# ------------------------------------------------------------------ idle / working
def test_idle_slots_are_never_touched_however_long(wd):
    f = wd
    f.v2.watchdog.tick(time.time() + 10 ** 6)
    assert f.mgr.keys == [] and f.mgr.killed == [] and screens(f) == []
    assert all(f.agent(s)['state'] == 'idle' for s in f.names)


def test_busy_worker_with_fresh_transcript_is_working(wd):
    f = wd
    dispatch_one(f)
    f.v2.watchdog.tick(time.time())
    assert f.agent('worker-1')['watch_state'] == 'working'
    assert ('idle', 'working') in transitions(f, 'worker-1') and keys(f, 'worker-1') == []


# ------------------------------------------------------------------ prompting
def test_whitelisted_dialog_is_answered_with_screen_saved(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM_LITERAL
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == ['Enter']
    assert f.agent('worker-1')['watch_state'] == 'prompting' and f.agent('worker-1')['state'] == 'busy'
    saved, = screens(f)
    assert saved.startswith('worker-1-g0-') and saved.endswith('prompt-dangerous_rm.txt')
    assert 'Dangerous rm' in (f.run_dir / 'watchdog' / saved).read_text()
    answered, = audits(f, 'watch.prompt_answered')
    assert answered['prompt'] == 'dangerous_rm' and json.loads(answered['keys']) == ['Enter']
    assert answered['authorized'] == 'rm build/tmp'
    assert not f.paused() and f.v2.bus.slot_state('worker-1')['status'] == 'received'
    f.mgr.screens[f.agent('worker-1')['pane']] = WORKING  # the dialog went away: back to work
    f.v2.watchdog.tick(time.time())
    assert f.agent('worker-1')['watch_state'] == 'working'


def test_same_dialog_three_times_becomes_stalled(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM_LITERAL
    now = time.time()
    for i in range(3):
        f.v2.watchdog.tick(now + i)
    assert keys(f, 'worker-1') == ['Enter'] * 3
    f.v2.watchdog.tick(now + 3)
    assert f.agent('worker-1')['watch_state'] == 'stalled' and keys(f, 'worker-1')[-1] == 'Escape'


def test_unknown_dialog_is_held_not_pressed(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_UNKNOWN
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == []
    assert f.agent('worker-1')['state'] == 'held' and f.paused()
    assert f.v2.bus.slot_state('worker-1')['status'] == 'uncertain'
    saved, = screens(f)
    assert saved.endswith('prompt-unknown.txt') and 'git push --force' in (f.run_dir / 'watchdog' / saved).read_text()
    assert audits(f, 'watch.prompt_unknown')[0]['marker'] == 'do you want to proceed?'


def test_trust_dialog_is_answered_while_starting(wd):
    f = wd
    f.setf('worker-2', state='starting')  # a new generation before READY: no delivery yet
    f.mgr.screens[f.agent('worker-2')['pane']] = TRUST.format(cwd(f, 'worker-2'))
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-2') == ['Enter'] and f.agent('worker-2')['watch_state'] == 'prompting'


def test_prompt_table_hot_reload_and_bad_edit_keeps_previous(wd, tmp_path):
    f = wd
    table = tmp_path / 'prompts.yaml'
    table.write_text('prompts: []\nunknown_markers: ["Do you want to proceed?"]\n')
    f.cfg['prompt_table'] = str(table)
    f.v2.watchdog = watchdog.Watchdog(f.v2)
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = 'Allow web fetch of ' + str(cwd(f, 'worker-1')) + '/page.html?\n❯ Yes'
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == []                     # not in the table: nothing pressed
    table.write_text('prompts:\n  - {name: web_fetch, match: ["allow web fetch"], keys: [Enter], verify: paths_in_run}\n')
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == ['Enter']               # edited table in force without restart
    assert json.loads(audits(f, 'watch.table_reloaded')[0]['prompts']) == ['web_fetch']
    table.write_text('prompts: [ {name: broken')
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == ['Enter', 'Enter']      # previous table still in force
    assert audits(f, 'watch.table_rejected')


def test_capture_failures_raise_one_alarm_per_streak(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = ''
    now = time.time()
    for i in range(5):
        f.v2.watchdog.tick(now + i)
    alarm, = audits(f, 'watch.capture_fail')
    assert alarm['slot'] == 'worker-1' and alarm['level'] == 'error'
    assert f.agent('worker-1')['capture_fail'] == '5'


# ------------------------------------------------------------------ stalled / frozen
def test_stall_sends_escape_without_holding_and_activity_resumes(wd):
    f = wd
    dispatch_one(f)
    t = time.time() + 700
    f.v2.watchdog.tick(t)
    assert f.agent('worker-1')['watch_state'] == 'stalled' and keys(f, 'worker-1') == ['Escape']
    assert f.agent('worker-1')['state'] == 'busy' and not f.paused()  # no hold: E16 stall != dead
    f.v2.watchdog.tick(t + 10)
    assert keys(f, 'worker-1') == ['Escape']   # spaced: grace/escapes = 100 s
    transcript = f.agent('worker-1')['transcript']
    os.utime(transcript, (t + 20, t + 20))     # the agent wrote again
    f.v2.watchdog.tick(t + 30)
    assert f.agent('worker-1')['watch_state'] == 'working' and f.agent('worker-1')['stall_escapes'] == '0'
    assert transitions(f, 'worker-1')[-2:] == [('idle', 'stalled'), ('stalled', 'working')]


def frozen_checks(f, old_pane, msg_id):
    assert old_pane in f.mgr.killed
    assert f.agent('worker-1')['generation'] == '1' and f.agent('worker-1')['label'] == 'worker-1.1'
    assert f.agent('worker-1')['state'] == 'starting' and f.agent('worker-1')['pane'] != old_pane
    assert (f.agent('worker-1')['pane'], old_pane) in f.mgr.tabbed       # next generation in place
    msg = f.v2.bus.message(msg_id)
    assert msg['status'] == 'committed' and msg['outcome'] == 'cancelled' and str(msg['stop_confirmed']) == '1'
    assert not f.v2.bus.slot_state('worker-1').get('current_delivery')
    assert not f.paused() and f.task()['status'] == 'running'
    report = f.v2.pending_text('supervisor')[0]
    assert report.startswith('[worker-1.1 report (took over from worker-1)]') and 'NOT re-run' in report
    restarted, = audits(f, 'watch.frozen_restarted')
    assert restarted['prev'] == 'worker-1' and restarted['label'] == 'worker-1.1'
    assert any(n.endswith('-frozen.txt') for n in screens(f))
    assert list((f.run_dir / 'handoffs').glob('worker-1-g0-frozen-*.md'))
    # The new generation starts normally.
    assert answer_turn(f, 'worker-1', 'READY', startup=True)['status'] == 'ready'
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['runtime_ready'] == '1'


def test_stalled_past_grace_is_frozen_killed_and_restarted(wd):
    f = wd
    dispatch_one(f)
    old, msg_id = f.agent('worker-1')['pane'], f.v2.bus.slot_state('worker-1')['message_id']
    t = time.time() + 700
    f.v2.watchdog.tick(t)
    f.mgr.screens[old] = WORKING + ' changing'      # screen moves, transcript does not
    f.v2.watchdog.tick(t + 150)
    assert old not in f.mgr.killed
    f.v2.watchdog.tick(t + 301)
    assert ('stalled', 'frozen') in transitions(f, 'worker-1')
    frozen_checks(f, old, msg_id)


def test_escapes_that_change_nothing_mean_frozen_before_grace(wd):
    f = wd
    dispatch_one(f)
    old, msg_id = f.agent('worker-1')['pane'], f.v2.bus.slot_state('worker-1')['message_id']
    t = time.time() + 700
    for dt in (0, 100, 200):
        f.v2.watchdog.tick(t + dt)
    assert keys(f, 'worker-1') == ['Escape'] * 3 and old not in f.mgr.killed
    f.v2.watchdog.tick(t + 210)   # < grace (300), but three Escapes left the same screen
    assert 'unchanged' in json.loads(audits(f, 'agent.state')[-1]['detail'])['reason']
    frozen_checks(f, old, msg_id)


def test_uninterruptible_process_twice_is_frozen(wd):
    f = wd
    dispatch_one(f)
    old, msg_id = f.agent('worker-1')['pane'], f.v2.bus.slot_state('worker-1')['message_id']
    f.mgr.stats[old] = ['Ss', 'U+']   # E5: autofs read, keys do nothing
    f.v2.watchdog.tick(time.time())
    assert old not in f.mgr.killed
    f.v2.watchdog.tick(time.time() + 1)
    assert 'uninterruptible' in audits(f, 'watch.frozen_restarted')[0]['reason']
    frozen_checks(f, old, msg_id)


def test_frozen_while_paused_is_held_not_restarted(wd):
    f = wd
    dispatch_one(f)
    old = f.agent('worker-1')['pane']
    f.pause('market closed')
    f.mgr.stats[old] = ['U']
    f.v2.watchdog.tick(time.time()); f.v2.watchdog.tick(time.time() + 1)
    assert f.mgr.killed == [] and f.agent('worker-1')['state'] == 'held'
    assert f.agent('worker-1')['generation'] == '0'


def test_adopted_pane_is_never_killed(wd, monkeypatch):
    f = wd
    dispatch_one(f)
    old = f.agent('worker-1')['pane']
    monkeypatch.setattr(subfleet, 'adopted', lambda cfg, slot, generation: slot == 'worker-1')
    f.mgr.stats[old] = ['U']
    f.v2.watchdog.tick(time.time()); f.v2.watchdog.tick(time.time() + 1)
    assert f.mgr.killed == [] and f.agent('worker-1')['state'] == 'held'


def test_frozen_supervisor_gets_its_message_back_with_a_note(wd):
    f = wd
    from test_v2_codex_claude import submit
    submit(f)
    old = f.agent('supervisor')['pane']
    original = typed(f, 'supervisor')[0]
    f.mgr.stats[old] = ['U']
    f.v2.watchdog.tick(time.time()); f.v2.watchdog.tick(time.time() + 1)
    assert old in f.mgr.killed and f.agent('supervisor')['generation'] == '1'
    again = f.v2.pending_text('supervisor')[0]
    assert original.split('\n')[0] in again and 'froze while handling this message' in again


def test_held_and_dead_slots_are_left_alone(wd):
    f = wd
    dispatch_one(f)
    f.v2.transaction('t-hold', lambda: f.v2.hold('worker-1', 'operator inspection'))
    f.mgr.stats[f.agent('worker-1')['pane']] = ['U']
    f.v2.watchdog.tick(time.time() + 10 ** 5); f.v2.watchdog.tick(time.time() + 10 ** 5 + 1)
    assert f.mgr.killed == [] and keys(f, 'worker-1') == []


# ------------------------------------------------------------------ v2 hold exit
REPORT = 'Done:\n- x\nEvidence:\n- worker-1/x.md\nNot done: none\nBlocked: none'


def test_blocked_task_hold_is_released_when_the_turn_ends(wd):
    f = wd
    dispatch_one(f)
    msg_id = f.v2.bus.slot_state('worker-1')['message_id']
    f.v2.finish('blocked', 'supervisor gave up')   # in-flight worker is held as uncertain (E16 ①)
    assert f.agent('worker-1')['state'] == 'held' and f.v2.bus.slot_state('worker-1')['status'] == 'uncertain'
    assert answer_turn(f, 'worker-1', REPORT)['status'] == 'released'
    assert f.agent('worker-1')['state'] == 'idle'
    assert f.v2.bus.slot_state('worker-1')['status'] == 'idle' and f.v2.bus.slot_state('worker-1')['pel_count'] == 0
    msg = f.v2.bus.message(msg_id)
    assert msg['outcome'] == 'cancelled' and str(msg['stop_confirmed']) == '1'     # result NOT applied
    assert f.r.llen(f.v2.bus.pending_key('supervisor')) == 0               # no report forwarded
    released, = audits(f, 'hold.released')
    assert released['slot'] == 'worker-1' and released['message_id'] == msg_id
    # The same Stop fired twice: nothing is held any more.
    assert answer_turn(f, 'worker-1', REPORT, prompt=typed(f, 'worker-1')[-1])['status'] == 'ignored'


def test_hold_exit_needs_the_held_delivery_own_turn(wd):
    f = wd
    dispatch_one(f)
    f.v2.finish('blocked', 'supervisor gave up')
    assert answer_turn(f, 'worker-1', REPORT, prompt='just chatting')['status'] == 'ignored'
    assert answer_turn(f, 'worker-1', REPORT, generation=4)['status'] == 'stale'
    assert f.agent('worker-1')['state'] == 'held' and f.v2.bus.slot_state('worker-1')['status'] == 'uncertain'


def test_forged_release_event_is_rejected(wd):
    f = wd
    dispatch_one(f)  # delivered, not held
    env = {k: f.v2.bus.message(f.v2.bus.slot_state('worker-1')['message_id'])[k] for k in runtime_v2.IDENTITY}
    runtime_v2.post_event(f.v2.bus, dict(kind='turn_ended', agent='worker-1', generation=0, run_id=f.v2.bus.run_id,
                                         thread=f.agent('worker-1')['thread'], request_id='h-forged', delivery=env),
                          'h-forged')
    assert f.v2.step(10)
    assert audits(f, 'hold.release_rejected')[0]['error'] == 'slot is not held'
    assert f.v2.bus.slot_state('worker-1')['status'] == 'received' and f.agent('worker-1')['state'] == 'busy'


# ------------------------------------------------------------------ dialog text is not an authorisation
def held_unpressed(f, slot, prompt, reason):
    assert keys(f, slot) == [] and f.agent(slot)['state'] == 'held' and f.paused()
    unverified, = audits(f, 'watch.prompt_unverified')
    assert unverified['prompt'] == prompt and reason in unverified['reason']
    assert any(n.endswith('prompt-' + prompt + '.txt') for n in screens(f))
    assert audits(f, 'watch.prompt_answered') == []


def test_dangerous_rm_on_a_variable_is_held_for_a_human(wd):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM   # rm -rf "$OUT/"*: OUT may be empty
    f.v2.watchdog.tick(time.time())
    held_unpressed(f, 'worker-1', 'dangerous_rm', 'shell variable')
    assert f.v2.bus.slot_state('worker-1')['status'] == 'uncertain'
    f.v2.watchdog.tick(time.time() + 1)                         # held: the next tick presses nothing either
    assert keys(f, 'worker-1') == []


@pytest.mark.parametrize('target', ['../worker-2/out', '/Users', '~/Documents'])
def test_dangerous_rm_outside_the_slot_directory_is_held(wd, target):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = DIALOG_RM_LITERAL.replace('build/tmp', target)
    f.v2.watchdog.tick(time.time())
    held_unpressed(f, 'worker-1', 'dangerous_rm', 'outside the slot directory')


def test_read_outside_inside_the_run_workspace_is_answered(wd):
    f = wd
    dispatch_one(f)
    shared = cwd(f, 'worker-1').parent / 'worker-2' / 'notes.md'   # <run>/workspace is granted (--add-dir)
    f.mgr.screens[f.agent('worker-1')['pane']] = READ.format(shared)
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-1') == ['Enter'] and f.agent('worker-1')['state'] == 'busy'
    assert audits(f, 'watch.prompt_answered')[0]['prompt'] == 'read_outside'


@pytest.mark.parametrize('screen,reason', [
    (READ.format('/Users/someone/.ssh/id_ed25519'), 'outside the granted area'),
    (READ.format('/Users/justin/Documents/projects/cmux-fleet/cmux_fleet/fleet.py'), 'outside the granted area'),
    ('Read outside the working directories?\n❯ 1. Yes', 'no path'),   # scope not on the screen
])
def test_read_outside_the_run_or_without_a_path_is_held(wd, screen, reason):
    f = wd
    dispatch_one(f)
    f.mgr.screens[f.agent('worker-1')['pane']] = screen
    f.v2.watchdog.tick(time.time())
    held_unpressed(f, 'worker-1', 'read_outside', reason)


@pytest.mark.parametrize('screen,name,reason', [
    (DIALOG_TRUST, 'trust_folder_legacy', 'outside the granted area'),   # some other folder than the slot's own
    ('Trust this folder?\n› 1. Yes', 'trust_folder_legacy', 'no path'),
    (CLAUDE_TRUST_LIVE, 'trust_folder', 'outside the granted area'),     # the live capture names another directory
])
def test_trust_of_a_foreign_or_unnamed_folder_is_held(wd, screen, name, reason):
    f = wd
    f.setf('worker-2', state='starting')
    f.mgr.screens[f.agent('worker-2')['pane']] = screen
    f.v2.watchdog.tick(time.time())
    held_unpressed(f, 'worker-2', name, reason)


def test_dialog_on_a_pane_outside_this_fleet_registry_is_not_answered(wd):
    f = wd
    dispatch_one(f)
    pane = f.agent('worker-1')['pane']
    f.mgr.screens[pane] = DIALOG_RM_LITERAL
    f.mgr.owns = lambda p: False
    f.v2.watchdog.tick(time.time())
    held_unpressed(f, 'worker-1', 'dangerous_rm', 'registry')


def test_live_claude_trust_dialog_wrapped_path_is_read_whole():
    table = watchdog.parse_table(watchdog.DEFAULT_TABLE.read_text())
    entry = watchdog.match_prompt(table, CLAUDE_TRUST_LIVE)[0]
    assert watchdog.authorize(entry, CLAUDE_TRUST_LIVE, 25, CLAUDE_TRUST_LIVE_CWD, '/nowhere')[0] is True
    assert watchdog.authorize(entry, CLAUDE_TRUST_LIVE, 25, CLAUDE_TRUST_LIVE_CWD + '-other', '/nowhere')[0] is False
    assert watchdog.authorize(entry, CLAUDE_TRUST_LIVE, 25, CLAUDE_TRUST_LIVE_CWD.rsplit('/', 1)[0], '/nowhere')[0] is False


def test_claude_trust_dialog_for_the_slot_own_directory_is_answered_down_enter(wd):
    f = wd
    f.setf('worker-2', state='starting')
    f.mgr.screens[f.agent('worker-2')['pane']] = claude_trust(str(cwd(f, 'worker-2')))
    f.v2.watchdog.tick(time.time())
    assert keys(f, 'worker-2') == ['Down', 'Enter'] and f.agent('worker-2')['state'] == 'starting'
    assert audits(f, 'watch.prompt_answered')[0]['prompt'] == 'trust_folder'
