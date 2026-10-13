"""Behaviour regressions for Codex delivery and settlement through `codex queue` (worker-2).

Owner: worker-2 (exclusive file; worker-1 commits it together with the implementation).
Interfaces: worker-1/implementation-plan.md v2 §2-§3:
- `cmux_fleet.codex_queue`: enqueue (test seam), deliver, bound, first_queue_turn, clear, withdraw, queued_rows;
- the `cq_*` delivery record on the agent hash;
- `fleet.notify` -> `tui_turn(turn=)`.
Nothing here is skipped: the new channel must pass.

Codex settlement is checked only through its real entry points (fleet.notify / codex_queue.bound).
The Claude/Pi text check `runtime_v2.turn_answers` is shared and deliberately not asserted here.

Real 0.162.1 facts used as inputs (worker-1 probe/home9d, worker-2 lab/evidence4):
- `task_started.turn_attribution.turn_trigger` is "user" or "queue";
- the queued input is an `item_completed` UserMessage with its `client_id`;
- the next queued turn starts ~2 ms after the previous `task_complete`;
- an idle daemon-mode TUI consumes the queued row before `codex queue` returns, so the
  client_id is often unreadable;
- notify input-messages accumulate;
- one turn-id can notify twice.
A queued item id is never a turn-id.
"""
import datetime
import json
import sqlite3
import sys
import threading
import time
import uuid

import pytest

from test_fleet import FakeMgr, write_rollout
from test_message_bus_v2 import private_redis_v2  # noqa: F401  (fixture)
from cmux_fleet import codex_queue, fleet, runtime_v2

WORKERS = ['worker-1', 'worker-2']
REPORT = 'Done:\n- {0}\nEvidence:\n- {1}\nNot done: none\nBlocked: none'
WAIT = json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': 'waiting', 'dispatch': []})
# Long enough that two report follow-ups share their first 120 normalised characters.
SHARED = 'compared the eight vendor quotes line by line against the frozen bill of materials and the approved budget sheet'
QUEUE_SCHEMA = """
CREATE TABLE IF NOT EXISTS queued_items (id TEXT PRIMARY KEY NOT NULL, thread_id TEXT NOT NULL,
    payload_json TEXT NOT NULL, queue_order INTEGER NOT NULL, created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS queued_items_thread_order_idx ON queued_items(thread_id, queue_order);
"""  # queued_items of codex-cli 0.162.1 (worker-1 probe/w2copy)


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
        'turn_attribution': {'turn_id': turn, 'turn_trigger': trigger, 'root_turn_id': turn}}}]
    for i, (client, text) in enumerate(users):
        rows.append({'timestamp': stamp(t0 + 5 + i), 'type': 'event_msg', 'payload': {
            'type': 'item_completed', 'turn_id': turn, 'item': {
                'type': 'UserMessage', 'id': uuid.uuid4().hex, 'client_id': client or '',
                'content': [{'type': 'text', 'text': text}]}}})
    if complete:
        rows.append({'timestamp': stamp(t0 + 50), 'type': 'event_msg', 'payload': {
            'type': 'task_complete', 'turn_id': turn, 'last_agent_message': answer}})
    return rows


# ---------------------------------------------------------------- channel stub (plan §6.1 seam)
class Queue:
    """Stands in for `codex queue`. Each message gets a unique queued id and a unique *real*
    client_id (the one the rollout will carry). `readable=False` models the measured idle case:
    the row was consumed before the CLI returned, so enqueue reports client_id ''. `sqlite=True`
    leaves the item pending in $CODEX_HOME/queue_1.sqlite (not consumed yet)."""

    def __init__(self):
        self.items, self.fail, self.readable, self.sqlite = [], None, True, False

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
                payload = {'UserInput': {'content': [{'type': 'text', 'text': text}], 'client_id': real}}
                con.execute('INSERT INTO queued_items VALUES (?,?,?,?,?,?)',
                            (item['queued_id'], thread, json.dumps(payload), order, now, now))
        return item


# Independent paste/keys call log, separate from the queue log (Queue.items) and from FakeMgr.sent.
PASTE_CALLS = []


@pytest.fixture(autouse=True)
def paste_spy(monkeypatch):
    PASTE_CALLS.clear()
    real_paste, real_key = FakeMgr.send_prompt_to_idle, FakeMgr.send_key

    def paste(self, pane, text, bracketed=False):
        PASTE_CALLS.append((pane, text))
        return real_paste(self, pane, text, bracketed)

    def key(self, pane, k):
        PASTE_CALLS.append((pane, '<key ' + k + '>'))
        return real_key(self, pane, k)
    monkeypatch.setattr(FakeMgr, 'send_prompt_to_idle', paste)
    monkeypatch.setattr(FakeMgr, 'send_key', key)


def consume_all(cfg):
    """The TUI took every pending row (it is now running)."""
    with sqlite3.connect(str(codex_queue.db_path(cfg))) as con:
        con.execute('DELETE FROM queued_items')


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
    pane = pane or f.agent(slot)['pane']
    return [text for p, text in f.mgr.sent if p == pane]


def queued(f, slot='supervisor'):
    thread = f.agent(slot).get('thread')
    return [q for q in f.q.items if q['thread'] == thread and q['name'] == slot]


def history(f):
    return ['startup prompt'] + [q['text'] for q in queued(f)]


def last(f):
    """(real client_id, text) the rollout of the in-flight delivery's own turn carries."""
    q = queued(f)[-1]
    return q['real_client'], q['text']


def codex_turn(f, answer, *, turn=None, trigger='queue', users=None, thread=None, generation=None,
               inputs=None, before=(), after=(), started_ms=None, late=None):
    """A finished supervisor turn: write its rollout, then call the real notify entry point.
    Defaults describe the turn that the in-flight delivery itself started."""
    gen = int(f.agent('supervisor')['generation']) if generation is None else generation
    thread = thread or f.test_threads['supervisor']
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


def claude_turn(f, slot, text, prompt=None):
    """A Claude worker's Stop hook (the worker side is unchanged by this work)."""
    thread = f.test_threads[slot]
    transcript = f.test_claude / slot / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    prompt = prompt if prompt is not None else typed(f, slot)[-1]
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': prompt}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    transcript.write_text('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    gen = str(f.agent(slot)['generation'])  # read before the hook thread: the test client has one connection
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


def no_input_box(f):
    pane = f.agent('supervisor')['pane']
    calls = [t for p, t in PASTE_CALLS if p == pane and t not in ('<key Escape>',)]
    return calls == [] and typed(f, 'supervisor') == [] and \
        not [k for p, k in f.mgr.keys if p == pane and k in ('Enter', 'C-m', 'Return')]


@pytest.fixture
def sup(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    """Codex supervisor + 2 Claude workers on a private Redis, everyone READY."""
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    r = private_redis_v2
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
    f.q = Queue()
    monkeypatch.setattr(codex_queue, 'enqueue', f.q.enqueue)
    f.start_all()
    r.set(f.v2.prefix + ':boot', 'running')
    # The first prompt is an argv argument (allowed); its READY turn is user-triggered.
    assert codex_turn(f, 'READY', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])['status'] == 'ready'
    for w in WORKERS:
        assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    yield f
    f.v2.close()


# ================================================================ A. completed_turn (fast callback)
def _rollout(tmp_path, rows):
    cwd = tmp_path / 'agents' / 'supervisor'
    cwd.mkdir(parents=True, exist_ok=True)
    thread, sessions = str(uuid.uuid4()), tmp_path / 'sessions'
    write_rollout(sessions, thread, cwd, rows=rows)
    return cwd, thread, sessions


def test_completed_turn_accepts_a_finished_turn_after_the_next_queued_turn_started(tmp_path):
    """The next queued turn starts ~2 ms after the previous task_complete (measured)."""
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


# ================================================================ B. settlement through fleet.notify
def test_delivery_turn_settles_and_nothing_is_typed(sup):
    f = sup
    submit(f)
    assert no_input_box(f)
    assert len(queued(f)) == 1 and queued(f)[0]['text'] == body_of(f)
    assert cq(f, 'cq_state') == 'queued'
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'
    assert cq(f, 'cq_state') == 'bound'


def test_manual_turn_with_delivery_only_in_history_is_not_settled(sup):
    """Root cause of the old text gate, checked at the Codex settlement entry (not turn_answers)."""
    f = sup
    submit(f)
    body = body_of(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'just chatting')],
                     inputs=['startup prompt', body, 'just chatting'])
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
    t1, first = str(uuid.uuid4()), [last(f)]
    assert codex_turn(f, plan(['worker-1'], 'check vendor C too'), turn=t1)['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format(SHARED + ' -- vendor C', 'worker-1/c.md'))['status'] == 'committed'
    f.pump()
    second = body_of(f)
    assert runtime_v2._norm(first[0][1])[:120] == runtime_v2._norm(second)[:120] and first[0][1] != second
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
    assert len(queued(f)) == n + 1 and queued(f)[-1]['text'].startswith('[worker-2 report]')
    assert no_input_box(f)


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
    assert no_input_box(f) and queued(f) == []


def test_v2_send_refuses_paste_fallback(sup):
    f = sup
    with pytest.raises(ValueError, match='fallback forbidden'):
        f.send('supervisor', f.agent('supervisor')['pane'], 'anything')


# ================================================================ D. codex_queue.bound (plan §2 rules)
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


def test_bound_accepts_only_the_queue_turn_that_carries_the_delivery(tmp_path):
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
    """Two deliveries with the same text: each binds only to the turn that carries its own client_id."""
    now = int(time.time() * 1000)
    rows = turn_rows('T1', 'queue', [('client-1', 'SAME')], 'OK', now + 20) + \
        turn_rows('T2', 'queue', [('client-2', 'SAME')], 'OK', now + 120)
    thread, sessions = _sessions(tmp_path, rows)
    r1, r2 = _rec(thread, 'SAME', 'client-1', now), _rec(thread, 'SAME', 'client-2', now + 100)
    assert codex_queue.bound(r1, thread, 'T1', sessions)[0] and codex_queue.bound(r2, thread, 'T2', sessions)[0]
    assert not codex_queue.bound(r1, thread, 'T2', sessions)[0]
    assert not codex_queue.bound(r2, thread, 'T1', sessions)[0]


def test_identical_text_without_client_id_never_binds_the_older_turn(tmp_path):
    """client_id unreadable (measured normal case) and the same text was delivered before:
    the older queue turn started 1 s before this delivery was queued; it must not bind."""
    now = int(time.time() * 1000)
    thread, sessions = _sessions(tmp_path, turn_rows('TOLD', 'queue', [('client-old', 'DELIVERY')], 'OK', now - 1000))
    ok, why = codex_queue.bound(_rec(thread, 'DELIVERY', '', now), thread, 'TOLD', sessions)
    assert ok is False, why


def test_identical_text_without_client_id_binds_only_the_first_queue_turn_after_queueing(tmp_path):
    """Two queue turns with the same text after this delivery was queued (e.g. the operator also
    queued it): without the unique id only the first can be this delivery."""
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


# ================================================================ E. end to end: ids, duplicates, withdraw races
def test_unreadable_client_id_still_settles_the_genuine_queue_turn(sup):
    """Idle TUI consumed the row before the CLI returned: cq_client_id is ''. The delivery's own
    queue turn (which carries the real client_id) still settles, exactly once."""
    f = sup
    f.q.readable = False
    submit(f)
    assert cq(f, 'cq_state') == 'queued' and cq(f, 'cq_client_id') == ''
    turn = str(uuid.uuid4())
    assert codex_turn(f, plan(['worker-1']), turn=turn)['status'] == 'committed'
    again = codex_turn(f, plan(['worker-2']), turn=turn, users=[last(f)])
    assert again is False or again['status'] == 'ignored', again


def test_unreadable_client_id_manual_turn_with_the_same_text_is_not_settled(sup):
    """Without the unique id the operator typing the delivery text verbatim must still not settle it."""
    f = sup
    f.q.readable = False
    submit(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', body_of(f))])
    assert out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_runtime_user_turn_absorbing_the_delivery_is_not_settled(sup):
    f = sup
    submit(f)
    client, text = last(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'operator question'), (client, text)])
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received' and typed(f, 'worker-1') == []


def test_runtime_turn_id_equal_to_queued_item_id_is_not_settled(sup):
    f = sup
    submit(f)
    qid = queued(f)[-1]['queued_id']
    client, text = last(f)
    thread, answer = f.test_threads['supervisor'], plan(['worker-1'])
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                  rows=turn_rows(str(uuid.uuid4()), 'queue', [(client, text)], answer))
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': qid,
                          'input-messages': history(f), 'last-assistant-message': answer})
    out = drive(f, lambda: fleet.notify(f.path, 'supervisor', '0', payload, f.test_sessions))
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'


def test_withdraw_before_consume_the_item_never_settles(sup):
    """clear() wins the race: the row is deleted before the TUI took it. Even if a queue turn with
    that text and client_id were reported afterwards, the withdrawn delivery is not settled."""
    f = sup
    f.q.sqlite = True
    submit(f)
    thread = f.test_threads['supervisor']
    assert len(codex_queue.queued_rows(f.cfg, thread)) == 1
    assert codex_queue.clear(f, 'supervisor', 'test withdraw') == 1
    assert codex_queue.queued_rows(f.cfg, thread) == [] and cq(f, 'cq_state') == 'withdrawn'
    out = codex_turn(f, plan(['worker-1']))
    assert out is False or out['status'] == 'ignored', out
    assert typed(f, 'worker-1') == [] and no_input_box(f)


def test_consume_before_withdraw_running_turn_is_not_settled(sup):
    """The TUI took the row first (already running); clear() then removes nothing but still marks
    the delivery withdrawn, so that turn's notify cannot settle it (fail closed, no paste)."""
    f = sup
    f.q.sqlite = True
    submit(f)
    consume_all(f.cfg)
    assert codex_queue.clear(f, 'supervisor', 'test withdraw') == 0
    assert cq(f, 'cq_state') == 'withdrawn'
    out = codex_turn(f, plan(['worker-1']))
    assert out is False or out['status'] == 'ignored', out
    assert typed(f, 'worker-1') == [] and no_input_box(f)


def test_identical_text_redelivery_is_not_settled_by_the_previous_turn(sup):
    """The same text is queued again (a retry/replay) after the first copy's turn already ran,
    with the id unreadable both times. The old turn's late notify must not settle the new copy."""
    f = sup
    f.q.readable = False
    submit(f)
    client, text = last(f)
    t_old, start_old = str(uuid.uuid4()), int(time.time() * 1000)
    old_rows = turn_rows(t_old, 'queue', [(client, text)], 'first answer', start_old)
    # the daemon queues the same text again for a fresh delivery record (what a replay does)
    time.sleep(.2)
    codex_queue.deliver(f, 'supervisor', 'replay-' + uuid.uuid4().hex[:6], text)
    assert cq(f, 'cq_state') == 'queued'
    write_rollout(f.test_sessions, f.test_threads['supervisor'], fleet.agent_cwd(f.cfg, 'supervisor'), rows=old_rows)
    ok, why = codex_queue.bound(codex_queue.record(f.r.hgetall(fleet.key('agent', 'supervisor'))),
                                f.test_threads['supervisor'], t_old, f.test_sessions)
    assert ok is False, why
    assert no_input_box(f)
