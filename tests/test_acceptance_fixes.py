"""Fixes from the real-cmux acceptance of 373d22c (docs/design.md §23).

Same two-fleet v2 world as test_subfleet / test_escalate: real private Redis, in-process daemons,
real ``claude_stop`` hooks, a fake cmux world.
"""
from pathlib import Path

from test_contracts import cf, inbox, report  # noqa: F401  (fixture + helpers)
from test_escalate import escalate, ok_check, parallel_m0  # noqa: F401
from test_subfleet import child_daemon, hook, no_launch, step, tree  # noqa: F401
from cmux_fleet import runtime_v2, subfleet


# ------------------------------------------------------------------ P1-3: startup READY
def test_ready_reply_takes_the_last_line():
    assert runtime_v2.ready_reply("I've read AGENTS.md. No task has been sent yet.\n\nREADY")
    assert runtime_v2.ready_reply('**READY**\n') and runtime_v2.ready_reply('READY.')
    assert not runtime_v2.ready_reply('NOT READY') and not runtime_v2.ready_reply('Ready to go')
    assert not runtime_v2.ready_reply('READY\nI will start now') and not runtime_v2.ready_reply('')


def test_startup_not_ready_gets_one_correction_then_blocks(tree):
    t, f = tree, tree.f
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 2, launch=no_launch)
    cf = child_daemon(t, rec)
    # a chatty but well-ended reply settles
    assert hook(t, cf, 'worker-1', "I've read AGENTS.md. No task has been sent yet.\n\nREADY",
                prompt='startup')['status'] == 'ready'
    # anything else: one correction typed into the pane, the slot keeps starting
    answer = hook(t, cf, 'worker-2', 'Ready to go', prompt='startup')
    assert answer['status'] == 'rejected' and 'asked once more' in answer['reason']
    pane = cf.agent('worker-2')['pane']
    assert [x for p, x in t.world.sent if p == pane][-1] == runtime_v2.STARTUP_NUDGE
    assert cf.agent('worker-2')['state'] == 'starting' and cf.task().get('status') != 'blocked'
    # the corrected turn settles normally
    assert hook(t, cf, 'worker-2', 'READY', prompt=runtime_v2.STARTUP_NUDGE)['status'] == 'ready'
    assert cf.agent('worker-2')['state'] == 'idle'


def test_second_bad_startup_reply_blocks(tree):
    t, f = tree, tree.f
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 1, launch=no_launch)
    cf = child_daemon(t, rec)
    assert hook(t, cf, 'worker-1', 'NOT READY', prompt='startup')['status'] == 'rejected'
    step(t)
    assert hook(t, cf, 'worker-1', 'still not', prompt=runtime_v2.STARTUP_NUDGE)['status'] == 'rejected'
    assert cf.agent('worker-1')['state'] == 'dead'
    assert cf.task()['status'] == 'blocked'


# ------------------------------------------------------------------ P1-4: sub-fleet attempts are capped
def old_attempts(run, task_id, n):
    for i in range(n):
        d = Path(run) / 'sub' / (task_id if i == 0 else task_id + '-attempt-' + str(i + 1))
        d.mkdir(parents=True)
        subfleet.save_record(d, {'task_id': task_id, 'slot': 'worker-1', 'state': 'failed', 'attempt': i + 1})


def test_escalate_after_max_attempts_fails_the_task(cf, ok_check):
    parallel_m0(cf)
    old_attempts(cf.run_dir, 'M0', 2)  # max_attempts defaults to 2
    report(cf, 'worker-1', escalate())
    row = cf.contract_state()['tasks']['M0']
    assert row['status'] == 'failed' and subfleet.EXHAUSTED in row['reason']
    up = inbox(cf, 'supervisor')[-1]
    assert 'max-attempts 2: sub-fleet M0 already started 2 time(s)' in up and 'marked failed' in up
    assert not inbox(cf, 'worker-1') and cf.escalation_state() == {}
    assert not (cf.run_dir / 'subfleet-requests').exists()  # no spawn was requested


def test_spawn_refused_once_attempts_are_used(tree):
    f = tree.f
    old_attempts(f.run_dir, 'M0', 1)
    rec = subfleet.spawn_child(f, 'worker-1', 'M0', 'Research five sources and merge them', 1, launch=no_launch)
    assert rec['attempt'] == 2 and subfleet.attempts_used(f.run_dir, 'M0') == 2
    reason = subfleet.check(f, 'worker-2', 'M0', 1, ready=False)  # also the cap, before any slot state
    assert reason.startswith('sub-fleet M0 is already running')
    subfleet.save_record(rec['run'], dict(subfleet.record(rec['run']), state='failed'))
    assert subfleet.check(f, 'worker-2', 'M0', 1, ready=False).startswith('max-attempts 2: sub-fleet M0 already')
