"""Acceptance: after the first prompt, Codex deliveries never go through the TUI input box.

Owner: worker-2 (exclusive file). Plan: worker-2/regression-plan.md.
Interfaces: worker-1/implementation-plan.md §3, route A, `codex queue`:
- `codex_queue.enqueue` is the test seam (§6.1);
- the delivery record is `cq_*` on the agent hash;
- a turn settles a delivery only if `codex_queue.bound` proves it from the rollout (§2 rules 1-6).

The CHANNEL ADAPTER block is the only code that knows how a delivery reaches Codex. Every other
helper writes rollouts in the real 0.162.1 shape (§6.2):
- `task_started.turn_attribution.turn_trigger` is "queue" or "user";
- one `item_completed` UserMessage per input, carrying its `client_id`;
- then `task_complete`.
A queued item id is never used as a turn-id. Measured facts used as inputs:
- notify input-messages accumulate;
- the same turn-id can notify twice;
- the next queued turn starts ~2 ms after the previous one completes;
- an idle TUI consumes the queued row before `codex queue` returns, so client_id is often empty
  (worker-2 lab/evidence4).
"""
import datetime
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from test_fleet import FakeMgr, write_rollout, fx  # noqa: F401  (fx fixture)
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from test_resume import managed, task_run, crash_and_resume  # noqa: F401  (fixtures)
import test_subfleet as ts
from cmux_fleet import codex_queue, fleet, runtime_v2, subfleet

WORKERS = ['worker-1', 'worker-2']
ENTER_KEYS = ('Enter', 'Return', 'C-m', 'KP_Enter')
REPORT = 'Done:\n- {0}\nEvidence:\n- {1}\nNot done: none\nBlocked: none'
WAIT = json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': 'waiting', 'dispatch': []})
SHARED = 'compared the eight vendor quotes line by line against the frozen bill of materials and the approved budget sheet'


# ---------------------------------------------------------------------------
# CHANNEL ADAPTER (route A, plan §3.1 / §6.1). Only this block knows the channel.
# ---------------------------------------------------------------------------
QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL,
    payload_json TEXT NOT NULL, queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS queued_items_thread_order_idx ON queued_items(thread_id, queue_order);
CREATE TABLE IF NOT EXISTS queued_thread_revisions (revision INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT NOT NULL UNIQUE);
CREATE TRIGGER IF NOT EXISTS queued_items_revision_after_insert AFTER INSERT ON queued_items BEGIN
    INSERT INTO queued_thread_revisions (thread_id) VALUES (NEW.thread_id) ON CONFLICT(thread_id) DO UPDATE
    SET revision = (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions); END;
CREATE TRIGGER IF NOT EXISTS queued_items_revision_after_delete AFTER DELETE ON queued_items BEGIN
    INSERT INTO queued_thread_revisions (thread_id) VALUES (OLD.thread_id) ON CONFLICT(thread_id) DO UPDATE
    SET revision = (SELECT COALESCE(MAX(revision), 0) + 1 FROM queued_thread_revisions); END;
"""  # queue_1.sqlite of codex-cli 0.162.1 (worker-1 probe/w2copy .schema; migrations table omitted)


class Channel:
    """Stands in for `codex queue`: records each message and returns a unique queued id and
    client_id (as the CLI + queued_items row would). It never types and never runs the real CLI.
    With sqlite=True it also leaves the item pending in $CODEX_HOME/queue_1.sqlite (a TUI that has
    not consumed it yet), so withdraw/clear can be checked against the real table."""

    def __init__(self, sqlite=False):
        self.items, self.fail, self.hook, self.sqlite = [], None, None, sqlite

    def enqueue(self, cfg, name, thread, text, **kw):
        if self.fail:
            raise codex_queue.QueueError(self.fail)
        item = {'queued_id': str(uuid.uuid4()), 'client_id': str(uuid.uuid4()), 'thread': thread}
        self.items.append(dict(item, name=name, text=text, run=str(cfg.get('run'))))
        if self.sqlite:
            path = codex_queue.db_path(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            con = sqlite3.connect(str(path))
            con.executescript(QUEUE_SCHEMA)
            order = con.execute('SELECT COALESCE(MAX(queue_order), -1) + 1 FROM queued_items WHERE thread_id=?', (thread,)).fetchone()[0]
            now = int(time.time() * 1000)
            payload = {'UserInput': {'content': [{'type': 'text', 'text': text, 'text_elements': []}], 'client_id': item['client_id']}}
            con.execute('INSERT INTO queued_items VALUES (?,?,?,?,?,?)',
                        (item['queued_id'], thread, json.dumps(payload), order, now, now))
            con.commit()
            con.close()
        if self.hook:
            self.hook(self.items[-1])
        return item


def pending_rows(cfg, thread=None):
    """Items still waiting in the real queue table (read through the product's own reader)."""
    return codex_queue.queued_rows(cfg, thread)


def install_channel(monkeypatch, channel=None):
    channel = channel or Channel()
    monkeypatch.setattr(codex_queue, 'enqueue', channel.enqueue)
    return channel


def injected(ch, thread=None, name=None):
    """Texts that reached Codex through the no-input-box channel (filtered by session / slot)."""
    return [i['text'] for i in ch.items if (thread is None or i['thread'] == thread) and (name is None or i['name'] == name)]


def break_channel(ch, reason='codex queue rc=1: simulated failure'):
    ch.fail = reason
# --------------------------- end of adapter --------------------------------


def stamp(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def turn_rows(turn, trigger, users, answer, started_ms=None, complete=True, thread=''):
    t0 = int(time.time() * 1000) if started_ms is None else started_ms
    rows = [{'timestamp': stamp(t0), 'type': 'event_msg', 'payload': {
        'type': 'task_started', 'turn_id': turn,
        'turn_attribution': {'turn_id': turn, 'turn_trigger': trigger, 'parent_turn_id': None, 'root_turn_id': turn}}}]
    for i, (client, text) in enumerate(users):
        rows.append({'timestamp': stamp(t0 + 5 + i), 'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'thread_id': thread, 'turn_id': turn, 'item': {
                'type': 'UserMessage', 'id': str(uuid.uuid4()), 'client_id': client or '',
                'content': [{'type': 'text', 'text': text, 'text_elements': []}]}}})
    if complete:
        rows.append({'timestamp': stamp(t0 + 50), 'type': 'event_msg', 'payload': {
            'type': 'task_complete', 'turn_id': turn, 'last_agent_message': answer}})
    return rows


def pasted(mgr_sent, pane):
    """Business text typed into a pane (key events of the World log are not text)."""
    return [t for p, t in mgr_sent if p == pane and not str(t).startswith('<key ')]


def enters(mgr_keys, pane):
    return [k for p, k in mgr_keys if p == pane and k in ENTER_KEYS]


def drive(f, fn):
    box = {}
    th = threading.Thread(target=lambda: box.setdefault('value', fn()))
    th.start()
    deadline = time.time() + 25
    while th.is_alive() and time.time() < deadline:
        f.v2.step(20)
    th.join(2)
    assert not th.is_alive()
    return box['value']


# ---------------------------------------------------------------- single fleet (Codex supervisor + 2 Claude)
def sup_thread(f):
    gen = int(f.agent('supervisor')['generation'])
    return f.test_threads['supervisor'] if gen == 0 else f.test_threads.setdefault(('supervisor', gen), str(uuid.uuid4()))


def in_flight(f):
    """(client_id, text) of the latest delivery queued for the supervisor's session."""
    items = [i for i in f.ch.items if i['thread'] == sup_thread(f) and i['name'] == 'supervisor']
    assert items, 'nothing was queued for the supervisor'
    return items[-1]['client_id'], items[-1]['text']


def history(f):
    return ['startup prompt'] + injected(f.ch, sup_thread(f), 'supervisor')


def codex_turn(f, answer, *, turn=None, trigger='queue', users=None, thread=None, generation=None,
               inputs=None, before=(), after=(), started_ms=None):
    """A finished supervisor turn: write its rollout, then call the real notify entry point."""
    gen = int(f.agent('supervisor')['generation']) if generation is None else generation
    thread = thread or sup_thread(f)
    turn = turn or str(uuid.uuid4())
    users = [in_flight(f)] if users is None else users
    rows = list(before) + turn_rows(turn, trigger, users, answer, started_ms, thread=thread) + list(after)
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'), rows=rows)
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                          'input-messages': history(f) if inputs is None else inputs, 'last-assistant-message': answer})
    return drive(f, lambda: fleet.notify(f.path, 'supervisor', str(gen), payload, f.test_sessions))


def startup_ready(f):
    """The first prompt is an argv argument (allowed); its READY turn is user-triggered."""
    return codex_turn(f, 'READY', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])


def claude_turn(f, slot, text, prompt=None):
    gen = int(f.agent(slot)['generation'])
    thread = f.test_threads[slot] if gen == 0 else f.test_threads.setdefault((slot, gen), str(uuid.uuid4()))
    transcript = f.test_claude / slot / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    prompt = prompt if prompt is not None else pasted(f.mgr.sent, f.agent(slot)['pane'])[-1]
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': prompt}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(f, lambda: fleet.claude_stop(f.path, slot, str(gen), raw))


def assert_no_input_box(f, pane=None):
    pane = pane or f.agent('supervisor')['pane']
    assert pasted(f.mgr.sent, pane) == [], 'delivery text was typed into the Codex input box'
    assert enters(f.mgr.keys, pane) == [], 'Enter was sent to the Codex pane'


def lease(f, slot='supervisor'):
    return f.v2.bus.slot_state(slot)


def body_of(f):
    return f.v2.bus.message(lease(f)['message_id'])['body']['text']


def audit(f):
    return [fields for _, fields in f.r.xrange(f.v2.bus.audit_key)]


def plan(workers, note='split the work'):
    return json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': note,
                       'dispatch': [{'to': w, 'instruction': 'Write ' + w + '/part.md with the findings.'} for w in workers]})


def submit(f, goal='build MAYFLY M0 parts'):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='task-' + uuid.uuid4().hex[:6], spec={'goal': goal}), 'new-task')
    assert f.v2.step(10)
    f.pump()


def build(r, tmp_path, monkeypatch, ready=True):
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=list(WORKERS), mode='task', supervisor_cli='codex', worker_cli='claude',
               codex_home=str(tmp_path / 'codex-home'), transport_version=2, allow_development_v2=True,
               stall_timeout=0, v2_lease_ms=300000, max_busy=0, codex=str(tmp_path / 'no-real-codex'))
    fleet.save(run / 'config.json', cfg)
    f = fleet.Fleet(run / 'config.json', r, FakeMgr(), sessions=tmp_path / 'codex-home' / 'sessions')
    f.test_sessions, f.test_claude = tmp_path / 'codex-home' / 'sessions', tmp_path / 'claude-projects'
    f.test_threads = {s: str(uuid.uuid4()) for s in ['supervisor'] + WORKERS}
    f.ch = install_channel(monkeypatch)
    f.start_all()
    r.set(f.v2.prefix + ':boot', 'running')
    if ready:
        assert startup_ready(f)['status'] == 'ready'
        for w in WORKERS:
            assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    return f


@pytest.fixture
def sup(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    f = build(private_redis_v2, tmp_path, monkeypatch)
    yield f
    f.v2.close()


@pytest.fixture
def booting(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    f = build(private_redis_v2, tmp_path, monkeypatch, ready=False)
    yield f
    f.v2.close()


# =========================================================================== 1. no paste / Enter
def test_first_prompt_never_typed(sup):
    assert_no_input_box(sup)
    assert sup.ch.items == []  # the start prompt is argv, not a delivery


def test_task_and_report_followups_bypass_input_box(sup):
    f = sup
    submit(f)
    assert codex_turn(f, plan(WORKERS))['status'] == 'committed'
    f.pump()
    for w in WORKERS:
        assert claude_turn(f, w, REPORT.format('wrote ' + w, w + '/part.md'))['status'] == 'committed'
        f.pump()
        assert codex_turn(f, WAIT)['status'] == 'committed'
        f.pump()
    assert_no_input_box(f)
    got = injected(f.ch, sup_thread(f), 'supervisor')
    # task + one follow-up per report (each once); anything after them (e.g. a format correction
    # for our minimal WAIT reply) also went through the queue, never the input box
    assert 'build MAYFLY M0 parts' in got[0], [g[:80] for g in got]
    assert [t.split(']')[0] for t in got[1:3]] == ['[' + w + ' report' for w in WORKERS], [g[:80] for g in got]
    assert len([t for t in got if t.startswith('[worker-')]) == 2
    assert any(e.get('event') == 'tui.queued' for e in audit(f))


def test_startup_nudge_bypasses_input_box(booting):
    f = booting
    for w in WORKERS:
        assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    out = codex_turn(f, 'Hello, how can I help?', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])
    assert out['status'] == 'rejected'
    f.v2.step(20)
    assert f.agent('supervisor')['state'] == 'starting'
    assert_no_input_box(f)
    assert injected(f.ch, name='supervisor') == [runtime_v2.STARTUP_NUDGE]
    assert codex_turn(f, 'READY')['status'] == 'ready'  # the nudge's own (bound) turn


def test_handoff_prompt_bypasses_input_box_and_is_offered_once(sup):
    f = sup
    f.start_rotation('supervisor', 90000, 100000)
    f.v2.step(20)
    f.pump(); f.pump()
    assert_no_input_box(f)
    assert len([t for t in injected(f.ch, name='supervisor') if 'same-task handoff' in t]) == 1


def test_v1_send_to_codex_supervisor_bypasses_input_box(fx, monkeypatch):  # noqa: F811
    f = fx
    ch = install_channel(monkeypatch)
    assert fleet.agent_cli(f.runtime_cfg('supervisor'), 'supervisor') == 'codex'
    pane = f.agent('supervisor')['pane']
    ok = f.send('supervisor', pane, 'v1 delivery to the Codex supervisor')
    assert pasted(f.mgr.sent, pane) == [] and enters(f.mgr.keys, pane) == []
    assert (ok and injected(ch) == ['v1 delivery to the Codex supervisor']) or (not ok and injected(ch) == [])


# =========================================================================== 2. fail closed
def test_failed_injection_holds_without_fallback_or_success(sup):
    f = sup
    break_channel(f.ch)
    submit(f)
    assert f.agent('supervisor')['state'] == 'held' and f.paused()
    assert lease(f)['status'] == 'uncertain'
    events = audit(f)
    assert not any(e.get('event') in ('tui.typed', 'tui.queued') and e.get('slot') == 'supervisor' for e in events)
    assert f.r.hget(fleet.key('agent', 'supervisor'), 'cq_state') == 'failed'
    for _ in range(3):
        f.pump(); f.v2.step(20); f.v2.renew_tui()
    assert_no_input_box(f)
    assert injected(f.ch) == []


def test_pane_gone_is_not_queued_and_not_typed(sup):
    """§5b: a queued item outlives the TUI and would run on a later resume, so nothing is queued."""
    f = sup
    f.mgr.alive.discard(f.agent('supervisor')['pane'])
    submit(f)
    assert f.agent('supervisor')['state'] == 'held' and lease(f)['status'] == 'uncertain'
    assert injected(f.ch) == []
    assert_no_input_box(f)


def test_failed_startup_nudge_does_not_fall_back_to_typing(booting):
    f = booting
    for w in WORKERS:
        assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    break_channel(f.ch)
    codex_turn(f, 'Hello?', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])
    f.v2.step(20)
    assert_no_input_box(f)
    assert injected(f.ch) == []


# =========================================================================== 3. binding
def test_cumulative_history_does_not_settle_a_manual_turn(sup):
    f = sup
    submit(f)
    body = body_of(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'just chatting')],
                     inputs=['startup prompt', body, 'just chatting'])
    assert out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_unissued_turn_does_not_settle_even_if_last_input_matches(sup):
    """Text equal, but the turn was typed by the operator (trigger user, no client_id)."""
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', body_of(f))])
    assert out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_other_thread_and_old_generation_never_settle(sup):
    f = sup
    submit(f)
    other = codex_turn(f, plan(['worker-1']), thread=str(uuid.uuid4()))
    assert other is False or other.get('status') in ('ignored', 'stale', 'rejected'), other
    assert codex_turn(f, plan(['worker-1']), generation=7)['status'] == 'stale'
    assert lease(f)['status'] == 'received'


def test_issued_turn_settles_once_even_if_notified_twice(sup):
    f = sup
    submit(f)
    turn, users = str(uuid.uuid4()), [in_flight(f)]
    assert codex_turn(f, plan(['worker-1']), turn=turn)['status'] == 'committed'
    again = codex_turn(f, plan(['worker-2']), turn=turn, users=users)
    assert again is False or again['status'] == 'ignored', again
    f.pump()
    assert len(pasted(f.mgr.sent, f.agent('worker-1')['pane'])) == 1
    assert pasted(f.mgr.sent, f.agent('worker-2')['pane']) == []


def test_duplicate_notify_of_old_turn_does_not_settle_the_next_delivery(sup):
    """Old turn notifies again (other payload) while the next delivery is queued. The next one
    shares its 120-char head with the old one; neither text nor history may bind it."""
    f = sup
    submit(f)
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format(SHARED + ' -- vendor A', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    t1, first = str(uuid.uuid4()), [in_flight(f)]
    assert codex_turn(f, plan(['worker-1'], 'check vendor C'), turn=t1)['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format(SHARED + ' -- vendor C', 'worker-1/c.md'))['status'] == 'committed'
    f.pump()
    assert runtime_v2._norm(first[0][1])[:120] == runtime_v2._norm(body_of(f))[:120]
    out = codex_turn(f, WAIT, turn=t1, users=first)
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_notify_after_next_turn_started_still_settles(sup):
    f = sup
    submit(f)
    nxt = turn_rows(str(uuid.uuid4()), 'user', [('', 'operator typed next')], '', complete=False)
    out = codex_turn(f, plan(['worker-1']), after=nxt)
    assert out is not False and out['status'] == 'committed', out


def test_merge_race_unknown_manual_turn_after_idle_check_is_rejected(sup):
    """The thread looked idle; the operator then started an unknown turn and the delivery was
    absorbed into it. The turn-id is new, it started after queueing, and it even carries the
    delivery's client_id as its last input. It must not settle; the delivery's own queue
    turn still settles afterwards."""
    f = sup
    submit(f)
    client, text = in_flight(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'operator: status?'), (client, text)])
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'
    assert pasted(f.mgr.sent, f.agent('worker-1')['pane']) == []
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'


def test_operator_input_merged_into_the_queue_turn_is_rejected(sup):
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), users=[in_flight(f), ('', 'operator steer')])
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_queued_item_id_is_never_taken_for_a_turn_id(sup):
    f = sup
    submit(f)
    qid = f.ch.items[-1]['queued_id']
    client, text = in_flight(f)
    thread, answer = sup_thread(f), plan(['worker-1'])
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                  rows=turn_rows(str(uuid.uuid4()), 'queue', [(client, text)], answer, thread=thread))
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': qid,
                          'input-messages': history(f), 'last-assistant-message': answer})
    out = drive(f, lambda: fleet.notify(f.path, 'supervisor', '0', payload, f.test_sessions))
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_fast_bind_callback_while_delivery_is_still_queuing(sup):
    """The turn ends and notify fires before `codex queue` has returned (cq_state 'queuing').
    tui_turn must wait for the record and then settle exactly once."""
    f = sup
    box = {}

    def turn_ends_before_cli_returns(item):
        turn, answer = str(uuid.uuid4()), plan(['worker-1'])
        thread = item['thread']
        write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                      rows=turn_rows(turn, 'queue', [(item['client_id'], item['text'])], answer, thread=thread))
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                              'input-messages': ['startup prompt', item['text']], 'last-assistant-message': answer})
        th = threading.Thread(target=lambda: box.setdefault('out', fleet.notify(f.path, 'supervisor', '0', payload, f.test_sessions)))
        th.start()
        box['thread'] = th
        time.sleep(1.0)  # the CLI is still "running"; the notify must not settle against 'queuing'
        box['state_during'] = f.r.hget(fleet.key('agent', 'supervisor'), 'cq_state')

    f.ch.hook = turn_ends_before_cli_returns
    submit(f)
    f.ch.hook = None
    deadline = time.time() + 25
    while box['thread'].is_alive() and time.time() < deadline:
        f.v2.step(20)
    box['thread'].join(2)
    assert box['state_during'] == 'queuing'
    assert box['out'] is not False and box['out']['status'] == 'committed', box['out']
    assert f.r.hget(fleet.key('agent', 'supervisor'), 'cq_state') == 'bound'


# =========================================================================== 4. busy / rotation / resume
def test_busy_supervisor_queues_followups_and_delivers_each_once(sup):
    f = sup
    submit(f)
    assert codex_turn(f, plan(WORKERS))['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format('wrote a', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    n = len(injected(f.ch, name='supervisor'))
    assert claude_turn(f, 'worker-2', REPORT.format('wrote b', 'worker-2/b.md'))['status'] == 'committed'
    f.pump(); f.pump()
    assert len(injected(f.ch, name='supervisor')) == n and f.r.llen(f.v2.bus.pending_key('supervisor')) == 1
    assert codex_turn(f, WAIT)['status'] == 'committed'
    f.pump(); f.pump()
    got = injected(f.ch, name='supervisor')
    assert len(got) == n + 1 and got[-1].startswith('[worker-2 report]')
    assert_no_input_box(f)


def test_old_generation_notify_after_resume_is_stale_and_brief_is_delivered_once(task_run, monkeypatch):  # noqa: F811
    state = task_run
    ch = install_channel(monkeypatch)
    f = state['f']
    f.ch = ch
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={
        'goal': 'write two parts', 'artifacts': [{'path': 'worker-1/a.md'}, {'path': 'worker-2/b.md'}]}), 'new-task')
    assert f.v2.step(10)
    f.pump()
    split = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/a.md with part A.'},
        {'to': 'worker-2', 'instruction': 'Write worker-2/b.md with part B.'}]})
    assert codex_turn(f, split)['status'] == 'committed'
    f.pump()
    (f.workspace() / 'worker-1').mkdir(exist_ok=True)
    (f.workspace() / 'worker-1' / 'a.md').write_text('part A')
    assert claude_turn(f, 'worker-1', REPORT.format('wrote worker-1/a.md', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    assert lease(f)['status'] == 'received'  # the report is in flight to the supervisor
    old_pane, old_thread = f.agent('supervisor')['pane'], sup_thread(f)
    f = crash_and_resume(state)
    f.ch = ch
    assert f.agent('supervisor')['generation'] != '0' and f.agent('supervisor')['pane'] != old_pane
    assert startup_ready(f)['status'] == 'ready'
    f.pump(); f.pump()
    briefs = [t for t in injected(ch, sup_thread(f), 'supervisor') if t.startswith('[Resumed task ')]
    assert len(briefs) == 1
    late = codex_turn(f, WAIT, thread=old_thread, generation=0, users=[('', 'x')])
    assert late is False or late['status'] == 'stale', late
    assert pasted(f.mgr.sent, f.agent('supervisor')['pane']) == [] and pasted(f.mgr.sent, old_pane) == []
    assert injected(ch, old_thread, 'supervisor')[-1].startswith('[worker-1 report]')  # nothing new for the old session


# =========================================================================== 4b. withdraw pending items (plan §3.4)
def test_finish_withdraws_a_pending_queued_item(sup):
    """A queued item that has not started must never run after the task ends (runtime_v2.finish)."""
    f = sup
    f.ch.sqlite = True
    submit(f)
    thread = sup_thread(f)
    assert [r['text'] for r in pending_rows(f.cfg, thread)] == [in_flight(f)[1]]
    f.v2.finish('stopped', 'operator stop')
    assert pending_rows(f.cfg, thread) == []
    assert f.r.hget(fleet.key('agent', 'supervisor'), 'cq_state') == 'withdrawn'
    assert_no_input_box(f)


def test_retire_withdraws_a_pending_queued_item(sup):
    """Retiring the session (rotation / stop path, runtime_v2.retire) removes its pending item."""
    f = sup
    f.ch.sqlite = True
    submit(f)
    thread = sup_thread(f)
    assert len(pending_rows(f.cfg, thread)) == 1
    f.v2.retire('supervisor', 'test retire')
    assert pending_rows(f.cfg, thread) == []
    assert f.r.hget(fleet.key('agent', 'supervisor'), 'cq_state') == 'withdrawn'
    assert_no_input_box(f)


def test_resume_withdraws_the_old_pending_item_and_delivers_once(task_run, monkeypatch):  # noqa: F811
    """Daemon crash while a report is queued but not started: --resume must remove the old
    session's pending item (it would otherwise run on its own) and deliver the brief once."""
    state = task_run
    ch = install_channel(monkeypatch, Channel(sqlite=True))
    f = state['f']
    f.ch = ch
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={
        'goal': 'write two parts', 'artifacts': [{'path': 'worker-1/a.md'}, {'path': 'worker-2/b.md'}]}), 'new-task')
    assert f.v2.step(10)
    f.pump()
    split = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/a.md with part A.'},
        {'to': 'worker-2', 'instruction': 'Write worker-2/b.md with part B.'}]})
    with sqlite3.connect(str(codex_queue.db_path(f.cfg))) as con:  # the task item was consumed by its turn
        con.execute('DELETE FROM queued_items')
    assert codex_turn(f, split)['status'] == 'committed'
    f.pump()
    (f.workspace() / 'worker-1').mkdir(exist_ok=True)
    (f.workspace() / 'worker-1' / 'a.md').write_text('part A')
    assert claude_turn(f, 'worker-1', REPORT.format('wrote worker-1/a.md', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    old_thread = sup_thread(f)
    assert [r['text'][:17] for r in pending_rows(f.cfg, old_thread)] == ['[worker-1 report]']
    f = crash_and_resume(state)
    f.ch = ch
    assert pending_rows(f.cfg, old_thread) == [], 'the retired session would still run the old report'
    assert startup_ready(f)['status'] == 'ready'
    f.pump(); f.pump()
    assert len([t for t in injected(ch, sup_thread(f), 'supervisor') if t.startswith('[Resumed task ')]) == 1
    assert pasted(f.mgr.sent, f.agent('supervisor')['pane']) == []


# =========================================================================== 5. subfleet adoption / restore (Codex)
def codex_hook(t, f, slot, text, *, users=None, trigger='queue', thread=None, generation=None):
    """A finished Codex turn in `slot` of fleet `f`, reported by that CLI's own notify argv."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    thread = thread or t.threads.setdefault((str(f.run_dir), slot, gen), str(uuid.uuid4()))
    if users is None:
        item = [i for i in t.ch.items if i['thread'] == thread][-1]
        users = [(item['client_id'], item['text'])]
    turn = str(uuid.uuid4())
    write_rollout(t.sessions, thread, fleet.agent_cwd(f.cfg, slot),
                  rows=turn_rows(turn, trigger, users, text, thread=thread))
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                          'input-messages': [u[1] for u in users], 'last-assistant-message': text})
    return ts.drive(t, lambda: fleet.notify(f.path, slot, str(gen), payload, t.sessions))


@pytest.fixture
def ctree(tmp_path, monkeypatch):
    """test_subfleet.tree with Codex workers (the adopted pane is a Codex session). Reuses its
    World/fixtures by import; tests/test_subfleet.py is not modified."""
    if shutil.which('redis-server') is None:
        pytest.skip('redis-server unavailable')
    root = Path('/tmp/cfq-' + uuid.uuid4().hex[:8])
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)
    world = ts.World(tmp_path / 'cmux-state')
    monkeypatch.setattr(fleet, 'manager_for', world.manager)
    rcfg, r = ts.start_private(root)
    run = tmp_path / 'fleet-20261013-000000-c0dec0de'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(rcfg, run=str(run), python=sys.executable, workers=['worker-1', 'worker-2'], mode='task',
               supervisor_cli='codex', worker_cli='codex', codex_home=str(tmp_path / 'codex-home'),
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0, max_attempts=2,
               name='研报', token='c0dec0de', codex=str(tmp_path / 'no-real-codex'),
               lifecycle='smoke')  # cli_pairs: Codex workers (needed for a Codex adopted pane) run v2 only as smoke
    fleet.save(run / 'config.json', cfg)
    t = SimpleNamespace(world=world, root=root, sessions=tmp_path / 'codex-home' / 'sessions',
                        claude=tmp_path / 'claude-projects', threads={}, fleets=[], tmp=tmp_path)
    t.ch = install_channel(monkeypatch)
    f = fleet.Fleet(run / 'config.json', r, world.manager(cfg), sessions=t.sessions)
    t.f = f
    t.fleets.append(f)
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    for slot in f.names:
        assert codex_hook(t, f, slot, 'READY', trigger='user', users=[('', 'startup')])['status'] == 'ready'
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': 'write the report'}), 'new-task')
    ts.step(t)
    yield t
    for x in t.fleets:
        try:
            x.v2.close()
        except Exception:
            pass
    for pid_file in root.glob('*/redis.pid'):
        try:
            os.kill(int(pid_file.read_text()), signal.SIGTERM)
        except (ProcessLookupError, ValueError):
            pass
    time.sleep(.2)
    shutil.rmtree(root, ignore_errors=True)


def test_subfleet_refuses_a_codex_worker_pane_before_queueing_or_typing(ctree):
    """Product guard (subfleet.check, subfleet.py:348): only Claude worker panes can be adopted.
    A Codex pane is refused before anything is queued or typed."""
    t, f = ctree, ctree.f
    sid = f.agent('worker-1')['pane']
    n = len(t.ch.items)
    with pytest.raises(subfleet.Refused, match='only official Claude Code worker panes'):
        ts.spawn_m0(t)
    assert len(t.ch.items) == n
    assert pasted(t.world.sent, sid) == [] and enters(t.world.keys, sid) == []
    assert f.agent('worker-1')['pane'] == sid and f.agent('worker-1')['state'] != 'delegated'


def lift_codex_adoption_guards(monkeypatch):
    """Two product guards refuse a Codex adopted pane today: subfleet.check (Claude panes only) and
    cli_pairs (a Codex+Codex child fleet runs v2 only as --lifecycle smoke). Lift both on purpose so
    worker-1's Codex adopt/restore branches (plan §3.2) can be exercised."""
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: None)
    real = subfleet.child_config
    monkeypatch.setattr(subfleet, 'child_config', lambda *a, **k: dict(real(*a, **k), lifecycle='smoke'))


def test_subfleet_adoption_and_restore_of_a_codex_pane_never_type(ctree, monkeypatch):
    """worker-1's Codex branches in subfleet.start_adopted / collect (plan §3.2). The product refuses
    Codex panes earlier (test above), so the guards are lifted here on purpose to exercise them."""
    t, f = ctree, ctree.f
    lift_codex_adoption_guards(monkeypatch)
    sid = f.agent('worker-1')['pane']
    parent_thread = t.threads[(str(f.run_dir), 'worker-1', 0)]
    rec, out = ts.spawn_m0(t)
    cf = ts.child_daemon(t, rec)
    assert cf.agent('supervisor')['pane'] == sid
    # adoption: the role change is queued into the adopted Codex session, never typed
    role = [i for i in t.ch.items if i['thread'] == parent_thread and 'sub-fleet M0' in i['text']]
    assert len(role) == 1 and 'READY' in role[0]['text']
    assert pasted(t.world.sent, sid) == [] and enters(t.world.keys, sid) == []
    # its READY turn is reported by the adopted CLI's own (parent worker-1) notify and lands in the child
    assert codex_hook(t, f, 'worker-1', 'READY', thread=parent_thread)['status'] == 'ready'
    assert cf.agent('supervisor')['runtime_ready'] == '1'
    for w in cf.workers:
        assert codex_hook(t, cf, w, 'READY', trigger='user', users=[('', 'startup')])['status'] == 'ready'
    ts.step(t)
    task = [i for i in t.ch.items if i['thread'] == parent_thread][-1]
    assert 'Research five sources and merge them' in task['text']
    assert pasted(t.world.sent, sid) == []
    # the child finishes; the parent hands the pane back and the restore prompt is queued too
    inside = Path(rec['run']) / 'workspace' / 'worker-1' / 'm0.md'
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('merged findings')
    cf.v2.finish('completed', 'all parts merged')
    cf.write_result()
    subfleet.tick(f)
    ts.stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'returned'
    back = [i for i in t.ch.items if i['thread'] == parent_thread and 'worker of the main fleet again' in i['text']]
    assert len(back) == 1
    assert pasted(t.world.sent, sid) == [] and enters(t.world.keys, sid) == []
    assert codex_hook(t, f, 'worker-1', 'READY', thread=parent_thread)['status'] == 'ready'
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['generation'] == '0'


def test_subfleet_adoption_queue_failure_blocks_without_typing(ctree, monkeypatch):
    t, f = ctree, ctree.f
    lift_codex_adoption_guards(monkeypatch)
    sid = f.agent('worker-1')['pane']
    rec, out = ts.spawn_m0(t)
    break_channel(t.ch)
    cf = ts.child_daemon(t, rec)
    assert pasted(t.world.sent, sid) == [] and enters(t.world.keys, sid) == []
    assert cf.agent('supervisor')['state'] == 'dead'
