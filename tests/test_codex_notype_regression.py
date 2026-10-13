"""Codex deliveries never use the TUI input box; turns settle only the delivery that started them.

Owner: worker-2. The single final test file for the `codex queue` channel; it replaces the earlier
`tests/test_codex_no_typing.py` (kept as evidence under worker-2/regression/).
Interfaces: worker-1/implementation-plan.md §0 I1-I7, §2, §3, §6 (module `cmux_fleet.codex_queue`).
Nothing here is skipped: every acceptance test runs against the product.

How a delivery reaches Codex is known only to the `Queue` stub (plan §6.1 seam: codex_queue.enqueue).
The stub keeps its own log of queued messages. A separate, independent log of real
send_prompt_to_idle / send_key calls (PASTE_CALLS / KEY_CALLS) proves the input box was never used.
Assertions about what was delivered check that the business body is contained in the queued text,
so a unique-delivery wrapper around the text (F1, authorized) does not change them. Rollouts always
carry exactly the text that was queued.

Real codex-cli 0.162.1 facts used as inputs (worker-1 probe/home9d, worker-2 lab/evidence4):
- `task_started.turn_attribution.turn_trigger` is "user" or "queue";
- a queued input is an `item_completed` UserMessage with its `client_id`;
- the next queued turn starts ~2 ms after the previous `task_complete`;
- an idle daemon-mode TUI consumes the queued row before `codex queue` returns, so the client_id
  is often unreadable;
- notify input-messages accumulate;
- one turn-id can notify twice;
- a queued item id never appears in the rollout and is never a turn-id.
The shared Claude/Pi text check `runtime_v2.turn_answers` is deliberately not asserted.
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
# Long enough that two report follow-ups share their first 120 normalised characters.
SHARED = 'compared the eight vendor quotes line by line against the frozen bill of materials and the approved budget sheet'
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


def plan(workers, note='split the work'):
    return json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': note,
                       'dispatch': [{'to': w, 'instruction': 'Write ' + w + '/part.md with the findings.'}
                                    for w in workers]})


# ---------------------------------------------------------------- rollout rows (real 0.162.1 shape)
def stamp(ms):
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def turn_rows(turn, trigger, users, answer, started_ms=None, complete=True):
    """task_started (with turn_attribution) + one UserMessage per (client_id, text) + task_complete."""
    t0 = int(time.time() * 1000) if started_ms is None else started_ms
    rows = [{'timestamp': stamp(t0), 'type': 'event_msg', 'payload': {
        'type': 'task_started', 'turn_id': turn,
        'turn_attribution': {'turn_id': turn, 'turn_trigger': trigger, 'parent_turn_id': None, 'root_turn_id': turn}}}]
    for i, (client, text) in enumerate(users):
        rows.append({'timestamp': stamp(t0 + 5 + i), 'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'turn_id': turn, 'item': {
                'type': 'UserMessage', 'id': uuid.uuid4().hex, 'client_id': client or '',
                'content': [{'type': 'text', 'text': text, 'text_elements': []}]}}})
    if complete:
        rows.append({'timestamp': stamp(t0 + 50), 'type': 'event_msg', 'payload': {
            'type': 'task_complete', 'turn_id': turn, 'last_agent_message': answer}})
    return rows


# ---------------------------------------------------------------- channel stub (plan §6.1 seam)
class Queue:
    """Stands in for `codex queue`; never runs the real CLI and never types.

    Each message gets a unique queued id and a unique *real* client_id (the one the rollout will
    carry). `readable=False` models the measured idle case (row consumed before the CLI returned,
    enqueue reports client_id ''). `sqlite=True` leaves the item pending in
    $CODEX_HOME/queue_1.sqlite. `hook(item)` runs while the CLI is still "running"."""

    def __init__(self, sqlite=False):
        self.items, self.fail, self.readable, self.sqlite, self.hook = [], None, True, sqlite, None

    def enqueue(self, cfg, name, thread, text, **kw):
        if self.fail:
            raise codex_queue.QueueError(self.fail)
        real = str(uuid.uuid4())
        item = {'queued_id': str(uuid.uuid4()), 'client_id': real if self.readable else '', 'thread': thread}
        self.items.append(dict(item, name=name, text=text, real_client=real))
        if self.sqlite:
            path = codex_queue.db_path(cfg)
            path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite3.connect(str(path)) as con:
                con.executescript(QUEUE_SCHEMA)
                order = con.execute('SELECT COALESCE(MAX(queue_order), -1) + 1 FROM queued_items WHERE thread_id=?',
                                    (thread,)).fetchone()[0]
                now = int(time.time() * 1000)
                payload = {'UserInput': {'content': [{'type': 'text', 'text': text, 'text_elements': []}], 'client_id': real}}
                con.execute('INSERT INTO queued_items VALUES (?,?,?,?,?,?)',
                            (item['queued_id'], thread, json.dumps(payload), order, now, now))
        if self.hook:
            self.hook(self.items[-1])
        return item


def install_queue(monkeypatch, q=None):
    q = q or Queue()
    monkeypatch.setattr(codex_queue, 'enqueue', q.enqueue)
    return q


def consume_all(cfg):
    """The TUI took every pending row (it is now running)."""
    with sqlite3.connect(str(codex_queue.db_path(cfg))) as con:
        con.execute('DELETE FROM queued_items')


# Independent log of real paste / key calls, separate from the queue log and from FakeMgr.sent
# (the shared conftest may record queued text in `sent`). Spied at class level, this module only.
PASTE_CALLS, KEY_CALLS = [], []


@pytest.fixture(autouse=True)
def paste_spy(monkeypatch):
    PASTE_CALLS.clear()
    KEY_CALLS.clear()
    for cls in (FakeMgr, ts.WorldMgr):
        real_paste, real_key = cls.send_prompt_to_idle, cls.send_key

        def paste(self, pane, text, bracketed=False, _real=real_paste):
            PASTE_CALLS.append((pane, text))
            return _real(self, pane, text, bracketed)

        def key(self, pane, k, _real=real_key):
            KEY_CALLS.append((pane, k))
            return _real(self, pane, k)
        monkeypatch.setattr(cls, 'send_prompt_to_idle', paste)
        monkeypatch.setattr(cls, 'send_key', key)


def input_box_use(pane, sent=()):
    """Everything that went into `pane`'s input box: real paste calls, Enter keys, and text in `sent`."""
    return ([t for p, t in PASTE_CALLS if p == pane] + [k for p, k in KEY_CALLS if p == pane and k in ENTER_KEYS]
            + [t for p, t in sent if p == pane and not str(t).startswith('<key ')])


def assert_no_input_box(f, pane=None):
    pane = pane or f.agent('supervisor')['pane']
    assert input_box_use(pane, f.mgr.sent) == [], 'the Codex input box was used'


# ---------------------------------------------------------------- harness
def drive(f, fn):
    """Run a blocking hook entry point while the daemon steps (as the real daemon would)."""
    box = {}
    thread = threading.Thread(target=lambda: box.setdefault('value', fn()))
    thread.start()
    deadline = time.time() + 25
    while thread.is_alive() and time.time() < deadline:
        f.v2.step(20)
    thread.join(2)
    assert not thread.is_alive()
    return box['value']


def typed(f, slot, pane=None):
    """Text that reached a (Claude worker) pane through the paste path."""
    pane = pane or f.agent(slot)['pane']
    return [text for p, text in f.mgr.sent if p == pane]


def session(f, slot):
    gen = int(f.agent(slot)['generation'])
    return f.test_threads[slot] if gen == 0 else f.test_threads.setdefault((slot, gen), str(uuid.uuid4()))


def queued(f, slot='supervisor', thread=None):
    thread = thread or f.agent(slot).get('thread')
    return [q for q in f.q.items if q['thread'] == thread and q['name'] == slot]


def queued_texts(f, slot='supervisor', thread=None):
    return [q['text'] for q in queued(f, slot, thread)]


def history(f):
    return ['startup prompt'] + queued_texts(f)


def last(f):
    """(real client_id, queued text) the rollout of the in-flight delivery's own turn carries."""
    q = queued(f)
    assert q, 'nothing was queued for the supervisor'
    return q[-1]['real_client'], q[-1]['text']


def codex_turn(f, answer, *, turn=None, trigger='queue', users=None, thread=None, generation=None,
               inputs=None, before=(), after=(), started_ms=None, late=None):
    """A finished supervisor turn: write its rollout, then call the real notify entry point.
    Defaults describe the turn that the in-flight delivery itself started."""
    gen = int(f.agent('supervisor')['generation']) if generation is None else generation
    thread = thread or session(f, 'supervisor')
    turn = turn or str(uuid.uuid4())
    users = [last(f)] if users is None else users
    cwd = fleet.agent_cwd(f.cfg, 'supervisor')
    rows = list(before) + turn_rows(turn, trigger, users, answer, started_ms) + list(after)
    if late is None:
        write_rollout(f.test_sessions, thread, cwd, rows=rows)
    else:  # notify fires before Codex flushes the rollout
        write_rollout(f.test_sessions, thread, cwd, rows=list(before) + rows[len(before):len(before) + 1])
        threading.Timer(late, lambda: write_rollout(f.test_sessions, thread, cwd, rows=rows)).start()
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                          'input-messages': history(f) if inputs is None else inputs, 'last-assistant-message': answer})
    return drive(f, lambda: fleet.notify(f.path, 'supervisor', str(gen), payload, f.test_sessions))


def startup_ready(f):
    """The first prompt is an argv argument (allowed); its READY turn is user-triggered."""
    return codex_turn(f, 'READY', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])


def claude_turn(f, slot, text, prompt=None):
    """A Claude worker's Stop hook (the worker side is unchanged by this work)."""
    gen = str(f.agent(slot)['generation'])  # read before the hook thread: the test client has one connection
    thread = session(f, slot)
    transcript = f.test_claude / slot / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    prompt = prompt if prompt is not None else typed(f, slot)[-1]
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': prompt}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(f, lambda: fleet.claude_stop(f.path, slot, gen, raw))


def lease(f, slot='supervisor'):
    return f.v2.bus.slot_state(slot)


def body_of(f, slot='supervisor'):
    return f.v2.bus.message(lease(f, slot)['message_id'])['body']['text']


def cq(f, field, slot='supervisor'):
    return f.r.hget(fleet.key('agent', slot), field)


def submit(f, goal='build MAYFLY M0 parts'):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='task-' + uuid.uuid4().hex[:6], spec={'goal': goal}), 'new-task')
    assert f.v2.step(10)
    f.pump()


def audit(f):
    return [fields for _, fields in f.r.xrange(f.v2.bus.audit_key)]


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
    f.q = install_queue(monkeypatch)
    f.start_all()
    r.set(f.v2.prefix + ':boot', 'running')
    if ready:
        assert startup_ready(f)['status'] == 'ready'
        for w in WORKERS:
            assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    return f


@pytest.fixture
def sup(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    """Codex supervisor + 2 Claude workers on a private Redis, everyone READY."""
    f = build(private_redis_v2, tmp_path, monkeypatch)
    yield f
    f.v2.close()


@pytest.fixture
def booting(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    f = build(private_redis_v2, tmp_path, monkeypatch, ready=False)
    yield f
    f.v2.close()


# ================================================================ A. completed_turn (fast callback, I5)
def _rollout(tmp_path, rows):
    cwd = tmp_path / 'agents' / 'supervisor'
    cwd.mkdir(parents=True, exist_ok=True)
    thread, sessions = str(uuid.uuid4()), tmp_path / 'sessions'
    write_rollout(sessions, thread, cwd, rows=rows)
    return cwd, thread, sessions


def test_completed_turn_accepts_a_finished_turn_after_the_next_queued_turn_started(tmp_path):
    t0 = int(time.time() * 1000)
    rows = turn_rows('T1', 'queue', [('c1', 'first')], 'ANSWER-1', t0) + \
        turn_rows('T2', 'queue', [('c2', 'second')], '', t0 + 52, complete=False)
    cwd, thread, sessions = _rollout(tmp_path, rows)
    data = {'thread-id': thread, 'turn-id': 'T1', 'last-assistant-message': 'ANSWER-1'}
    assert fleet.completed_turn(cwd, data, sessions) is True


def test_completed_turn_still_rejects_unfinished_wrong_and_foreign_turns(tmp_path):
    t0 = int(time.time() * 1000)
    rows = turn_rows('T1', 'queue', [('c1', 'first')], 'ANSWER-1', t0) + \
        turn_rows('T2', 'queue', [('c2', 'second')], '', t0 + 52, complete=False)
    cwd, thread, sessions = _rollout(tmp_path, rows)
    ask = lambda turn, text, where=cwd: fleet.completed_turn(where, {  # noqa: E731
        'thread-id': thread, 'turn-id': turn, 'last-assistant-message': text}, sessions)
    assert not ask('T2', '')                       # still running
    assert not ask('T9', 'ANSWER-1')               # unknown turn
    assert not ask('T1', 'OTHER')                  # other answer
    assert not ask('T1', 'ANSWER-1', tmp_path / 'elsewhere')  # other cwd
    assert not fleet.completed_turn(cwd, {'thread-id': 'not-a-uuid', 'turn-id': 'T1',
                                          'last-assistant-message': 'ANSWER-1'}, sessions)


# ================================================================ B. every entry avoids the input box (I7)
def test_first_prompt_is_argv_and_never_typed(sup):
    f = sup
    assert_no_input_box(f)
    assert f.q.items == []  # the start prompt is an argv argument, not a queued delivery
    spawned = [kw for kw in f.mgr.spawned if str(kw.get('prompt_file', '')).endswith('/supervisor-g0.md')]
    assert len(spawned) == 1, [kw.get('prompt_file') for kw in f.mgr.spawned]
    prompt = Path(spawned[0]['prompt_file']).read_text()
    assert prompt.strip()
    # the actor hands that prompt to codex as its last argv argument (the one allowed first prompt)
    argv = fleet.actor_argv(f.path, f.cfg, 'supervisor', 0, prompt)
    assert argv[-1] == prompt and 'queue' not in argv[:2]


def test_delivery_turn_settles_and_nothing_is_typed(sup):
    f = sup
    submit(f)
    assert_no_input_box(f)
    assert len(queued(f)) == 1 and body_of(f) in queued(f)[0]['text']
    assert cq(f, 'cq_state') == 'queued'
    assert any(e.get('event') == 'tui.queued' and e.get('slot') == 'supervisor' for e in audit(f))
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'
    assert cq(f, 'cq_state') == 'bound'


def test_task_and_report_followups_bypass_input_box_each_once(sup):
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
    got = queued_texts(f)
    assert 'build MAYFLY M0 parts' in got[0], [g[:80] for g in got]
    for w in WORKERS:  # one follow-up per report (a later format correction may follow; also queued)
        assert len([t for t in got if '[' + w + ' report]' in t]) == 1, [g[:80] for g in got]


def test_startup_nudge_is_queued_not_typed_and_its_turn_binds(booting):
    f = booting
    for w in WORKERS:
        assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    out = codex_turn(f, 'Hello, how can I help?', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])
    assert out['status'] == 'rejected'
    f.v2.step(20)
    assert f.agent('supervisor')['state'] == 'starting'
    assert_no_input_box(f)
    nudges = queued_texts(f)
    assert len(nudges) == 1 and runtime_v2.STARTUP_NUDGE in nudges[0]
    assert codex_turn(f, 'READY')['status'] == 'ready'  # the nudge's own (bound) turn


def test_failed_startup_nudge_does_not_fall_back_to_typing(booting):
    f = booting
    for w in WORKERS:
        assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    f.q.fail = 'codex queue rc=1: simulated'
    codex_turn(f, 'Hello?', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])
    f.v2.step(20)
    assert_no_input_box(f)
    assert f.q.items == []


def test_handoff_prompt_is_queued_once_not_typed(sup):
    f = sup
    f.start_rotation('supervisor', 90000, 100000)
    f.v2.step(20)
    f.pump(); f.pump()
    assert_no_input_box(f)
    assert len([t for t in queued_texts(f) if 'same-task handoff' in t]) == 1


def test_v1_send_to_codex_supervisor_is_queued_not_typed(fx, monkeypatch):  # noqa: F811
    f = fx
    q = install_queue(monkeypatch)
    assert fleet.agent_cli(f.runtime_cfg('supervisor'), 'supervisor') == 'codex'
    pane = f.agent('supervisor')['pane']
    ok = f.send('supervisor', pane, 'v1 delivery to the Codex supervisor')
    assert input_box_use(pane, f.mgr.sent) == []
    texts = [i['text'] for i in q.items]
    assert (ok and len(texts) == 1 and 'v1 delivery to the Codex supervisor' in texts[0]) or (not ok and texts == [])


# ================================================================ C. fail closed, never fall back to paste
def test_failed_delivery_holds_and_is_never_retried_by_paste(sup):
    f = sup
    f.q.fail = 'codex queue rc=1: simulated'
    submit(f)
    assert f.agent('supervisor')['state'] == 'held' and f.paused()
    assert lease(f)['status'] == 'uncertain' and cq(f, 'cq_state') == 'failed'
    assert not any(e.get('event') in ('tui.typed', 'tui.queued') and e.get('slot') == 'supervisor' for e in audit(f))
    for _ in range(3):
        f.pump(); f.v2.step(20); f.v2.renew_tui()
    assert_no_input_box(f)
    assert f.q.items == []


def test_pane_gone_is_not_queued_and_not_typed(sup):
    """Plan §5b: a queued item outlives the TUI and would run on a later resume, so nothing is queued."""
    f = sup
    f.mgr.alive.discard(f.agent('supervisor')['pane'])
    submit(f)
    assert f.agent('supervisor')['state'] == 'held' and lease(f)['status'] == 'uncertain'
    assert f.q.items == []
    assert_no_input_box(f)


def test_v2_send_refuses_paste_fallback(sup):
    f = sup
    with pytest.raises(ValueError, match='fallback forbidden'):
        f.send('supervisor', f.agent('supervisor')['pane'], 'anything')


# ================================================================ D. settlement through fleet.notify (I4)
def test_manual_turn_with_delivery_only_in_history_is_not_settled(sup):
    """Root cause of the old text gate, checked at the Codex settlement entry (not turn_answers)."""
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'just chatting')],
                     inputs=['startup prompt', last(f)[1], 'just chatting'])
    assert out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'
    assert typed(f, 'worker-1') == []


def test_late_duplicate_notify_of_an_old_turn_does_not_settle_a_same_head_delivery(sup):
    f = sup
    submit(f)
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format(SHARED + ' -- vendor A', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    t1, first, first_body = str(uuid.uuid4()), [last(f)], body_of(f)
    assert codex_turn(f, plan(['worker-1'], 'check vendor C too'), turn=t1)['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format(SHARED + ' -- vendor C', 'worker-1/c.md'))['status'] == 'committed'
    f.pump()
    second = body_of(f)
    assert runtime_v2._norm(first_body)[:120] == runtime_v2._norm(second)[:120] and first_body != second
    out = codex_turn(f, WAIT, turn=t1, users=first)
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received' and body_of(f) == second


def test_duplicate_notify_of_a_settled_turn_settles_nothing_else(sup):
    f = sup
    submit(f)
    turn, users = str(uuid.uuid4()), [last(f)]
    assert codex_turn(f, plan(['worker-1']), turn=turn)['status'] == 'committed'
    f.pump()
    again = codex_turn(f, plan(['worker-2'], 'changed my mind'), turn=turn, users=users)
    assert again is False or again['status'] == 'ignored', again
    f.pump()
    assert len(typed(f, 'worker-1')) == 1 and typed(f, 'worker-2') == []


def test_old_generation_notify_is_stale(sup):
    f = sup
    submit(f)
    assert codex_turn(f, plan(['worker-1']), generation=3)['status'] == 'stale'
    assert lease(f)['status'] == 'received'


def test_other_thread_notify_never_settles(sup):
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), thread=str(uuid.uuid4()))
    assert out is False or out['status'] in ('ignored', 'stale', 'rejected'), out
    assert lease(f)['status'] == 'received' and typed(f, 'worker-1') == []


def test_notify_for_finished_turn_settles_even_if_next_turn_already_started(sup):
    f = sup
    submit(f)
    nxt = turn_rows(str(uuid.uuid4()), 'user', [('', 'operator typed next')], '', complete=False)
    out = codex_turn(f, plan(['worker-1']), after=nxt)
    assert out is not False and out['status'] == 'committed', out


def test_notify_before_rollout_flush_settles_within_retry_window(sup):
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), late=.5)
    assert out is not False and out['status'] == 'committed', out


def test_fast_bind_callback_while_delivery_is_still_queuing(sup):
    """The turn ends and notify fires before `codex queue` has returned (cq_state 'queuing').
    tui_turn must wait for the record and then settle exactly once (I2/I4)."""
    f = sup
    box = {}

    def turn_ends_before_cli_returns(item):
        turn, answer = str(uuid.uuid4()), plan(['worker-1'])
        thread = item['thread']
        write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                      rows=turn_rows(turn, 'queue', [(item['real_client'], item['text'])], answer))
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                              'input-messages': ['startup prompt', item['text']], 'last-assistant-message': answer})
        th = threading.Thread(target=lambda: box.setdefault('out', fleet.notify(f.path, 'supervisor', '0', payload, f.test_sessions)))
        th.start()
        box['thread'] = th
        time.sleep(1.0)  # the CLI is still "running"; the notify must not settle against 'queuing'
        box['state_during'] = f.r.hget(fleet.key('agent', 'supervisor'), 'cq_state')

    f.q.hook = turn_ends_before_cli_returns
    submit(f)
    f.q.hook = None
    deadline = time.time() + 25
    while box['thread'].is_alive() and time.time() < deadline:
        f.v2.step(20)
    box['thread'].join(2)
    assert box['state_during'] == 'queuing'
    assert box['out'] is not False and box['out']['status'] == 'committed', box['out']
    assert cq(f, 'cq_state') == 'bound'


def test_busy_supervisor_gets_followups_one_at_a_time_and_each_once(sup):
    f = sup
    submit(f)
    assert codex_turn(f, plan(WORKERS))['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format('wrote a', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    n = len(queued(f))
    assert claude_turn(f, 'worker-2', REPORT.format('wrote b', 'worker-2/b.md'))['status'] == 'committed'
    f.pump(); f.pump()
    assert len(queued(f)) == n and f.r.llen(f.v2.bus.pending_key('supervisor')) == 1
    assert codex_turn(f, WAIT)['status'] == 'committed'
    f.pump(); f.pump()
    assert len(queued(f)) == n + 1 and '[worker-2 report]' in queued(f)[-1]['text']
    assert_no_input_box(f)


# ================================================================ E. codex_queue.bound (plan §2 rules, I3)
def _rec(thread, text, client, queued_ms, **kw):
    rec = {'cq_delivery': 'd-' + uuid.uuid4().hex[:8], 'cq_text': text, 'cq_thread': thread,
           'cq_queued_at': str(queued_ms), 'cq_state': 'queued', 'cq_queued_id': str(uuid.uuid4()),
           'cq_client_id': client, 'cq_turn': ''}
    rec.update(kw)
    return rec


def _sessions(tmp_path, rows):
    cwd = tmp_path / 'agents' / 'supervisor'
    cwd.mkdir(parents=True, exist_ok=True)
    thread, sessions = str(uuid.uuid4()), tmp_path / 'sessions'
    write_rollout(sessions, thread, cwd, rows=rows)
    return thread, sessions


def test_bound_accepts_the_queue_turn_that_carries_the_delivery_with_or_without_client_id(tmp_path):
    now = int(time.time() * 1000)
    thread, sessions = _sessions(tmp_path, turn_rows('TQ', 'queue', [('client-1', 'DELIVERY')], 'OK', now + 100))
    assert codex_queue.bound(_rec(thread, 'DELIVERY', 'client-1', now), thread, 'TQ', sessions) == (True, '')
    assert codex_queue.bound(_rec(thread, 'DELIVERY', '', now), thread, 'TQ', sessions) == (True, '')  # id unreadable


def test_race_idle_check_then_user_turn_absorbs_the_delivery_is_rejected(tmp_path):
    """Looked idle, the operator started an unknown turn, the delivery was absorbed into it:
    new turn-id, started after queueing, even carrying the delivery's client_id. Rejected (rule 2)."""
    now = int(time.time() * 1000)
    rows = turn_rows('TU', 'user', [('', 'operator question'), ('client-1', 'DELIVERY')], 'answer to operator', now + 50)
    thread, sessions = _sessions(tmp_path, rows)
    for client in ('client-1', ''):
        ok, why = codex_queue.bound(_rec(thread, 'DELIVERY', client, now), thread, 'TU', sessions)
        assert ok is False, why


def test_race_operator_input_merged_into_the_queue_turn_is_rejected(tmp_path):
    now = int(time.time() * 1000)
    rows = turn_rows('TQ', 'queue', [('client-1', 'DELIVERY'), ('', 'operator steer')], 'mixed answer', now + 50)
    thread, sessions = _sessions(tmp_path, rows)
    ok, why = codex_queue.bound(_rec(thread, 'DELIVERY', 'client-1', now), thread, 'TQ', sessions)
    assert ok is False, why


def test_queued_item_id_is_not_a_turn_id(tmp_path):
    now = int(time.time() * 1000)
    thread, sessions = _sessions(tmp_path, turn_rows('TQ', 'queue', [('client-1', 'DELIVERY')], 'OK', now + 50))
    rec = _rec(thread, 'DELIVERY', 'client-1', now)
    ok, why = codex_queue.bound(rec, thread, rec['cq_queued_id'], sessions)
    assert ok is False, why


def test_unique_client_id_decides_between_identical_texts(tmp_path):
    now = int(time.time() * 1000)
    rows = turn_rows('T1', 'queue', [('client-1', 'SAME')], 'OK', now + 20) + \
        turn_rows('T2', 'queue', [('client-2', 'SAME')], 'OK', now + 120)
    thread, sessions = _sessions(tmp_path, rows)
    r1, r2 = _rec(thread, 'SAME', 'client-1', now), _rec(thread, 'SAME', 'client-2', now + 100)
    assert codex_queue.bound(r1, thread, 'T1', sessions)[0] and codex_queue.bound(r2, thread, 'T2', sessions)[0]
    assert not codex_queue.bound(r1, thread, 'T2', sessions)[0]
    assert not codex_queue.bound(r2, thread, 'T1', sessions)[0]


def test_identical_text_without_client_id_never_binds_the_older_turn(tmp_path):
    now = int(time.time() * 1000)
    thread, sessions = _sessions(tmp_path, turn_rows('TOLD', 'queue', [('client-old', 'DELIVERY')], 'OK', now - 1000))
    ok, why = codex_queue.bound(_rec(thread, 'DELIVERY', '', now), thread, 'TOLD', sessions)
    assert ok is False, why


def test_identical_text_without_client_id_binds_only_the_first_queue_turn_after_queueing(tmp_path):
    now = int(time.time() * 1000)
    rows = turn_rows('TA', 'queue', [('c-a', 'DELIVERY')], 'OK', now + 10) + \
        turn_rows('TB', 'queue', [('c-b', 'DELIVERY')], 'OK', now + 100)
    thread, sessions = _sessions(tmp_path, rows)
    rec = _rec(thread, 'DELIVERY', '', now)
    assert codex_queue.bound(rec, thread, 'TA', sessions)[0] is True
    assert codex_queue.bound(rec, thread, 'TB', sessions)[0] is False


def test_bound_rejects_other_thread_edited_text_old_turn_rebinding_and_closed_states(tmp_path):
    now = int(time.time() * 1000)
    rows = turn_rows('TQ', 'queue', [('client-1', 'DELIVERY edited by operator')], 'OK', now + 50) + \
        turn_rows('TOLD', 'queue', [('client-1', 'DELIVERY')], 'OK', now - 60000)
    thread, sessions = _sessions(tmp_path, rows)
    rec = _rec(thread, 'DELIVERY', 'client-1', now)
    assert codex_queue.bound(rec, str(uuid.uuid4()), 'TQ', sessions)[0] is False          # rule 1
    assert codex_queue.bound(rec, thread, 'TQ', sessions)[0] is False                     # rule 3 (text edited)
    assert codex_queue.bound(rec, thread, 'TOLD', sessions)[0] is False                   # rule 5 (older turn)
    assert codex_queue.bound(dict(rec, cq_turn='TX', cq_state='bound'), thread, 'TQ', sessions)[0] is False  # rule 6
    for state in ('withdrawn', 'failed', 'queuing', ''):
        assert codex_queue.bound(dict(rec, cq_state=state), thread, 'TQ', sessions)[0] is False


# ================================================================ F. end to end: ids, duplicates, merge races
def test_unreadable_client_id_still_settles_the_genuine_queue_turn(sup):
    """Positive F1 case: the idle TUI consumed the row before the CLI returned, cq_client_id is ''.
    The delivery's own queue turn still settles, exactly once."""
    f = sup
    f.q.readable = False
    submit(f)
    assert cq(f, 'cq_state') == 'queued' and cq(f, 'cq_client_id') == ''
    turn = str(uuid.uuid4())
    assert codex_turn(f, plan(['worker-1']), turn=turn)['status'] == 'committed'
    assert cq(f, 'cq_state') == 'bound'
    again = codex_turn(f, plan(['worker-2']), turn=turn, users=[last(f)])
    assert again is False or again['status'] == 'ignored', again


def test_unreadable_client_id_manual_turn_with_the_same_text_is_not_settled(sup):
    f = sup
    f.q.readable = False
    submit(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', last(f)[1])])
    assert out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_identical_text_redelivery_is_not_settled_by_the_previous_turn(sup):
    """The same text is queued again (retry/replay) after the first copy's turn ran, id unreadable both
    times. The old turn must not settle the new copy (no cross-binding without the unique id)."""
    f = sup
    f.q.readable = False
    submit(f)
    client, text = last(f)
    t_old, start_old = str(uuid.uuid4()), int(time.time() * 1000)
    old_rows = turn_rows(t_old, 'queue', [(client, text)], 'first answer', start_old)
    time.sleep(.2)
    codex_queue.deliver(f, 'supervisor', 'replay-' + uuid.uuid4().hex[:6], text)
    assert cq(f, 'cq_state') == 'queued'
    write_rollout(f.test_sessions, session(f, 'supervisor'), fleet.agent_cwd(f.cfg, 'supervisor'), rows=old_rows)
    ok, why = codex_queue.bound(codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor'))),
                                session(f, 'supervisor'), t_old, f.test_sessions)
    assert ok is False, why
    assert_no_input_box(f)


def test_merge_race_unknown_manual_turn_absorbing_the_delivery_is_not_settled(sup):
    """End to end: after the idle check an unknown operator turn absorbed the delivery (new turn-id,
    started after queueing, its last input even carries the delivery's client_id). Not settled;
    the delivery's own queue turn still settles afterwards."""
    f = sup
    submit(f)
    client, text = last(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'operator question'), (client, text)])
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received' and typed(f, 'worker-1') == []
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'


def test_operator_input_merged_into_the_queue_turn_is_not_settled(sup):
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), users=[last(f), ('', 'operator steer')])
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_runtime_turn_id_equal_to_queued_item_id_is_not_settled(sup):
    f = sup
    submit(f)
    qid = queued(f)[-1]['queued_id']
    client, text = last(f)
    thread, answer = session(f, 'supervisor'), plan(['worker-1'])
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                  rows=turn_rows(str(uuid.uuid4()), 'queue', [(client, text)], answer))
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': qid,
                          'input-messages': history(f), 'last-assistant-message': answer})
    out = drive(f, lambda: fleet.notify(f.path, 'supervisor', '0', payload, f.test_sessions))
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


# ================================================================ G. withdraw vs consume, finish / retire / resume (I6)
def test_withdraw_before_consume_the_item_never_settles(sup):
    f = sup
    f.q.sqlite = True
    submit(f)
    thread = session(f, 'supervisor')
    assert len(codex_queue.queued_rows(f.cfg, thread)) == 1
    assert codex_queue.clear(f, 'supervisor', 'test withdraw') == 1
    assert codex_queue.queued_rows(f.cfg, thread) == [] and cq(f, 'cq_state') == 'withdrawn'
    out = codex_turn(f, plan(['worker-1']))
    assert out is False or out['status'] == 'ignored', out
    assert typed(f, 'worker-1') == []
    assert_no_input_box(f)


def test_consume_before_withdraw_running_turn_is_not_settled(sup):
    f = sup
    f.q.sqlite = True
    submit(f)
    consume_all(f.cfg)
    assert codex_queue.clear(f, 'supervisor', 'test withdraw') == 0
    assert cq(f, 'cq_state') == 'withdrawn'
    out = codex_turn(f, plan(['worker-1']))
    assert out is False or out['status'] == 'ignored', out
    assert typed(f, 'worker-1') == []
    assert_no_input_box(f)


def test_finish_withdraws_a_pending_queued_item(sup):
    f = sup
    f.q.sqlite = True
    submit(f)
    thread = session(f, 'supervisor')
    assert [r['text'] for r in codex_queue.queued_rows(f.cfg, thread)] == [last(f)[1]]
    f.v2.finish('stopped', 'operator stop')
    assert codex_queue.queued_rows(f.cfg, thread) == [] and cq(f, 'cq_state') == 'withdrawn'
    assert_no_input_box(f)


def test_retire_withdraws_a_pending_queued_item(sup):
    """Retiring the session (rotation / stop path, runtime_v2.retire) removes its pending item."""
    f = sup
    f.q.sqlite = True
    submit(f)
    thread = session(f, 'supervisor')
    assert len(codex_queue.queued_rows(f.cfg, thread)) == 1
    f.v2.retire('supervisor', 'test retire')
    assert codex_queue.queued_rows(f.cfg, thread) == [] and cq(f, 'cq_state') == 'withdrawn'
    assert_no_input_box(f)


def _report_in_flight(state, q):
    """task_run: a task split over two workers, worker-1 reported, its report is queued to the supervisor."""
    f = state['f']
    f.q = q
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={
        'goal': 'write two parts', 'artifacts': [{'path': 'worker-1/a.md'}, {'path': 'worker-2/b.md'}]}), 'new-task')
    assert f.v2.step(10)
    f.pump()
    split = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': 'worker-1', 'instruction': 'Write worker-1/a.md with part A.'},
        {'to': 'worker-2', 'instruction': 'Write worker-2/b.md with part B.'}]})
    if q.sqlite:
        consume_all(f.cfg)  # the task item was consumed by its own turn
    assert codex_turn(f, split)['status'] == 'committed'
    f.pump()
    (f.workspace() / 'worker-1').mkdir(exist_ok=True)
    (f.workspace() / 'worker-1' / 'a.md').write_text('part A')
    assert claude_turn(f, 'worker-1', REPORT.format('wrote worker-1/a.md', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    assert lease(f)['status'] == 'received'
    return f


def test_resume_new_generation_gets_the_brief_once_and_old_notify_is_stale(task_run, monkeypatch):  # noqa: F811
    state = task_run
    q = install_queue(monkeypatch)
    f = _report_in_flight(state, q)
    old_pane, old_thread = f.agent('supervisor')['pane'], session(f, 'supervisor')
    f = crash_and_resume(state)
    f.q = q
    assert f.agent('supervisor')['generation'] != '0' and f.agent('supervisor')['pane'] != old_pane
    assert startup_ready(f)['status'] == 'ready'
    f.pump(); f.pump()
    assert len([t for t in queued_texts(f) if '[Resumed task ' in t]) == 1
    late = codex_turn(f, WAIT, thread=old_thread, generation=0, users=[('', 'x')])
    assert late is False or late['status'] == 'stale', late
    assert input_box_use(f.agent('supervisor')['pane'], f.mgr.sent) == [] and input_box_use(old_pane, f.mgr.sent) == []
    assert '[worker-1 report]' in queued_texts(f, thread=old_thread)[-1]  # nothing new for the old session


def test_resume_withdraws_the_old_pending_item_and_delivers_once(task_run, monkeypatch):  # noqa: F811
    """Crash while the report is queued but not started: --resume removes the old session's pending
    item (it would otherwise run on its own) and the new generation gets the brief once."""
    state = task_run
    q = install_queue(monkeypatch, Queue(sqlite=True))
    f = _report_in_flight(state, q)
    old_thread = session(f, 'supervisor')
    assert ['[worker-1 report]' in r['text'] for r in codex_queue.queued_rows(f.cfg, old_thread)] == [True]
    f = crash_and_resume(state)
    f.q = q
    assert codex_queue.queued_rows(f.cfg, old_thread) == [], 'the retired session would still run the old report'
    assert startup_ready(f)['status'] == 'ready'
    f.pump(); f.pump()
    assert len([t for t in queued_texts(f) if '[Resumed task ' in t]) == 1
    assert input_box_use(f.agent('supervisor')['pane'], f.mgr.sent) == []


# ================================================================ H. subfleet adoption / restore (Codex pane)
def codex_hook(t, f, slot, text, *, users=None, trigger='queue', thread=None, generation=None):
    """A finished Codex turn in `slot` of fleet `f`, reported by that CLI's own notify argv."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    thread = thread or t.threads.setdefault((str(f.run_dir), slot, gen), str(uuid.uuid4()))
    if users is None:
        item = [i for i in t.q.items if i['thread'] == thread][-1]
        users = [(item['real_client'], item['text'])]
    turn = str(uuid.uuid4())
    write_rollout(t.sessions, thread, fleet.agent_cwd(f.cfg, slot), rows=turn_rows(turn, trigger, users, text))
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                          'input-messages': [u[1] for u in users], 'last-assistant-message': text})
    return ts.drive(t, lambda: fleet.notify(f.path, slot, str(gen), payload, t.sessions))


@pytest.fixture
def ctree(tmp_path, monkeypatch):
    """test_subfleet.tree with Codex workers (the adoptable pane is a Codex session). Reuses its
    World/helpers by import; tests/test_subfleet.py is not modified. Codex workers run v2 only as
    `--lifecycle smoke` (cli_pairs), so the parent uses it."""
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
               name='研报', token='c0dec0de', codex=str(tmp_path / 'no-real-codex'), lifecycle='smoke')
    fleet.save(run / 'config.json', cfg)
    t = SimpleNamespace(world=world, root=root, sessions=tmp_path / 'codex-home' / 'sessions',
                        claude=tmp_path / 'claude-projects', threads={}, fleets=[], tmp=tmp_path)
    t.q = install_queue(monkeypatch)
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


def lift_codex_adoption_guards(monkeypatch):
    """Two product guards refuse a Codex adopted pane today: subfleet.check (Claude panes only) and
    cli_pairs (a Codex+Codex child fleet runs v2 only as --lifecycle smoke). Lift both on purpose
    so worker-1's Codex adopt/restore branches (plan §3.2) are exercised."""
    monkeypatch.setattr(subfleet, 'check', lambda *a, **k: None)
    real = subfleet.child_config
    monkeypatch.setattr(subfleet, 'child_config', lambda *a, **k: dict(real(*a, **k), lifecycle='smoke'))


def test_subfleet_refuses_a_codex_worker_pane_before_queueing_or_typing(ctree):
    """Reachable product behaviour: only Claude worker panes can be adopted (subfleet.check)."""
    t, f = ctree, ctree.f
    sid = f.agent('worker-1')['pane']
    n = len(t.q.items)
    with pytest.raises(subfleet.Refused, match='only official Claude Code worker panes'):
        ts.spawn_m0(t)
    assert len(t.q.items) == n
    assert input_box_use(sid, t.world.sent) == []
    assert f.agent('worker-1')['pane'] == sid and f.agent('worker-1')['state'] != 'delegated'


def test_subfleet_adoption_and_restore_of_a_codex_pane_are_queued_never_typed(ctree, monkeypatch):
    t, f = ctree, ctree.f
    lift_codex_adoption_guards(monkeypatch)
    sid = f.agent('worker-1')['pane']
    parent_thread = t.threads[(str(f.run_dir), 'worker-1', 0)]
    rec, out = ts.spawn_m0(t)
    cf = ts.child_daemon(t, rec)
    assert cf.agent('supervisor')['pane'] == sid
    role = [i for i in t.q.items if i['thread'] == parent_thread and 'sub-fleet M0' in i['text']]
    assert len(role) == 1 and 'READY' in role[0]['text']
    assert input_box_use(sid, t.world.sent) == []
    # its READY turn is reported by the adopted CLI's own (parent worker-1) notify and lands in the child
    assert codex_hook(t, f, 'worker-1', 'READY', thread=parent_thread)['status'] == 'ready'
    assert cf.agent('supervisor')['runtime_ready'] == '1'
    for w in cf.workers:
        assert codex_hook(t, cf, w, 'READY', trigger='user', users=[('', 'startup')])['status'] == 'ready'
    ts.step(t)
    task = [i for i in t.q.items if i['thread'] == parent_thread][-1]
    assert 'Research five sources and merge them' in task['text']
    assert input_box_use(sid, t.world.sent) == []
    inside = Path(rec['run']) / 'workspace' / 'worker-1' / 'm0.md'
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('merged findings')
    cf.v2.finish('completed', 'all parts merged')
    cf.write_result()
    subfleet.tick(f)
    ts.stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'returned'
    back = [i for i in t.q.items if i['thread'] == parent_thread and 'worker of the main fleet again' in i['text']]
    assert len(back) == 1
    assert input_box_use(sid, t.world.sent) == []
    assert codex_hook(t, f, 'worker-1', 'READY', thread=parent_thread)['status'] == 'ready'
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['generation'] == '0'


def test_subfleet_adoption_queue_failure_blocks_without_typing(ctree, monkeypatch):
    t, f = ctree, ctree.f
    lift_codex_adoption_guards(monkeypatch)
    sid = f.agent('worker-1')['pane']
    rec, out = ts.spawn_m0(t)
    t.q.fail = 'codex queue rc=1: simulated'
    cf = ts.child_daemon(t, rec)
    assert input_box_use(sid, t.world.sent) == []
    assert cf.agent('supervisor')['state'] == 'dead'
