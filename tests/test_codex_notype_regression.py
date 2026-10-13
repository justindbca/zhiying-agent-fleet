"""Behaviour regressions for Codex supervisor delivery and settlement (worker-2, standalone).

Written by worker-2, integrated into tests/ by worker-1. Interfaces: worker-1/implementation-plan.md §2-§3
(route A, `codex queue`):
- the delivery record `cq_*` on the agent hash;
- `codex_queue.enqueue` / `deliver` / `bound` / `turn_record`;
- `fleet.notify` -> `tui_turn(turn=)`.

Sections:
- A + B + C use only entry points that exist on the baseline too (fleet.completed_turn,
  fleet.notify -> tui_turn, claude_stop, Fleet/RuntimeV2 with FakeMgr). The channel is reached
  through one stub: when cmux_fleet.codex_queue exists, its `enqueue` (the plan's test seam)
  records the message and returns a unique queued id and client_id. On the baseline there is no
  stub and the delivery is a paste. So the same acceptance assertions run on both trees.
- D tests `codex_queue.bound` and the queue path directly. It skips (never fails) while the
  module does not exist. A red test is always a real assertion, never an import error.

Rollouts use the real 0.162.1 shape (worker-1 probe/home9d, worker-2 lab):
- `task_started.turn_attribution.turn_trigger` is "user" or "queue";
- the queued input is an `item_completed` UserMessage with its `client_id`;
- the next queued turn starts ~2 ms after the previous `task_complete`;
- notify `input-messages` accumulates over the thread;
- the same turn-id can notify twice.

A queued item id (CLI output) is never a turn-id. Binding is by client_id + rollout attribution.
"""
import datetime
import importlib.util
import json
import sys
import threading
import time
import uuid

import pytest

from test_fleet import FakeMgr, write_rollout  # noqa: E402
from test_message_bus_v2 import private_redis_v2  # noqa: E402,F401  (fixture)
from cmux_fleet import fleet, runtime_v2  # noqa: E402

HAS_QUEUE = importlib.util.find_spec('cmux_fleet.codex_queue') is not None
WORKERS = ['worker-1', 'worker-2']
REPORT = 'Done:\n- {0}\nEvidence:\n- {1}\nNot done: none\nBlocked: none'
WAIT = json.dumps({'status': 'RUNNING', 'completion': 50, 'reason': 'waiting', 'dispatch': []})
# Long enough that two report follow-ups share their first 120 normalised characters.
SHARED = 'compared the eight vendor quotes line by line against the frozen bill of materials and the approved budget sheet'


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
    """Messages that went through the queue stub for this slot's thread (empty on the baseline)."""
    thread = f.agent(slot).get('thread')
    return [q for q in f.test_queue if q['thread'] == thread]


def delivered(f, slot='supervisor'):
    """Every text that reached the slot, typed or queued (for exactly-once counts)."""
    return typed(f, slot) + [q['text'] for q in queued(f, slot)]


def history(f, slot='supervisor'):
    """Cumulative input-messages as real Codex reports them."""
    return ['startup prompt'] + delivered(f, slot)


def last_delivery(f):
    """(client_id, text, trigger) of the delivery now in flight to the supervisor, by whatever route."""
    q = queued(f)
    if q:
        return q[-1]['client_id'], q[-1]['text'], 'queue'
    return '', typed(f, 'supervisor')[-1], 'user'


def codex_turn(f, answer, *, turn=None, trigger=None, users=None, thread=None, generation=None,
               inputs=None, before=(), after=(), started_ms=None, late=None):
    """A finished supervisor turn: write its rollout, then call the real notify entry point.

    Defaults describe the turn the in-flight delivery itself started."""
    gen = int(f.agent('supervisor')['generation']) if generation is None else generation
    thread = thread or f.test_threads['supervisor']
    turn = turn or str(uuid.uuid4())
    if users is None or trigger is None:
        client, text, how = last_delivery(f)
        users = [(client, text)] if users is None else users
        trigger = how if trigger is None else trigger
    inputs = history(f) if inputs is None else inputs
    cwd = fleet.agent_cwd(f.cfg, 'supervisor')
    rows = list(before) + turn_rows(turn, trigger, users, answer, started_ms) + list(after)
    if late is None:
        write_rollout(f.test_sessions, thread, cwd, rows=rows)
    else:  # notify fires before Codex flushes the rollout
        write_rollout(f.test_sessions, thread, cwd, rows=list(before) + rows[len(before):len(before) + 1])
        threading.Timer(late, lambda: write_rollout(f.test_sessions, thread, cwd, rows=rows)).start()
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn,
                          'input-messages': inputs, 'last-assistant-message': answer})
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


def submit(f, goal='build MAYFLY M0 parts'):
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': goal}), 'new-task')
    assert f.v2.step(10)
    f.pump()


def audit(f):
    return [fields for _, fields in f.r.xrange(f.v2.bus.audit_key)]


def install_queue_stub(f, monkeypatch, fail=None):
    """Plan §3.1 test seam: replace codex_queue.enqueue. No-op on the baseline."""
    f.test_queue = []
    if not HAS_QUEUE:
        return
    from cmux_fleet import codex_queue

    def enqueue(cfg, name, thread, text, **kw):
        if fail:
            raise codex_queue.QueueError(fail)
        item = {'queued_id': str(uuid.uuid4()), 'client_id': str(uuid.uuid4()), 'thread': thread}
        f.test_queue.append(dict(item, text=text))
        return item
    monkeypatch.setattr(codex_queue, 'enqueue', enqueue)


def build(r, tmp_path, monkeypatch):
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    r.flushdb()
    run = tmp_path / ('run-' + uuid.uuid4().hex)
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(run=str(run), redis_socket=r.connection_pool.connection_kwargs['path'], python=sys.executable,
               workers=list(WORKERS), mode='task', supervisor_cli='codex', worker_cli='claude',
               codex_home=str(tmp_path / 'codex-home'), transport_version=2, allow_development_v2=True,
               stall_timeout=0, v2_lease_ms=300000, max_busy=0, codex=str(tmp_path / 'no-codex-binary'))
    fleet.save(run / 'config.json', cfg)
    f = fleet.Fleet(run / 'config.json', r, FakeMgr(), sessions=tmp_path / 'codex-home' / 'sessions')
    f.test_sessions, f.test_claude = tmp_path / 'codex-home' / 'sessions', tmp_path / 'claude-projects'
    f.test_threads = {s: str(uuid.uuid4()) for s in ['supervisor'] + WORKERS}
    install_queue_stub(f, monkeypatch)
    f.start_all()
    r.set(f.v2.prefix + ':boot', 'running')
    # The first prompt is an argv argument (allowed); its READY turn is user-triggered.
    assert codex_turn(f, 'READY', trigger='user', users=[('', 'startup prompt')], inputs=['startup prompt'])['status'] == 'ready'
    for w in WORKERS:
        assert claude_turn(f, w, 'READY', prompt='startup prompt')['status'] == 'ready'
    return f


@pytest.fixture
def sup(private_redis_v2, tmp_path, monkeypatch):  # noqa: F811
    """Codex supervisor + 2 Claude workers on a private Redis, everyone READY."""
    f = build(private_redis_v2, tmp_path, monkeypatch)
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
    """Measured (worker-1 probe/home9d): the next queued turn's task_started is written ~2 ms
    after the previous task_complete. So when notify(T1) is checked, T2 has usually begun.
    Baseline: RED. completed_turn returns False on the first task_started it meets (fleet.py:626)."""
    t0 = int(time.time() * 1000)
    rows = turn_rows('T1', 'queue', [('c1', 'first')], 'ANSWER-1', t0) + \
        turn_rows('T2', 'queue', [('c2', 'second')], '', t0 + 52, complete=False)
    cwd, thread, sessions = _rollout(tmp_path, rows)
    data = {'thread-id': thread, 'turn-id': 'T1', 'last-assistant-message': 'ANSWER-1'}
    assert fleet.completed_turn(cwd, data, sessions) is True


def test_completed_turn_still_rejects_unfinished_wrong_and_foreign_turns(tmp_path):
    """Guards for any fix of the test above. Baseline: GREEN."""
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
    """Acceptance target: the delivery reaches Codex without the input box and its own turn settles.
    Baseline: RED (the delivery is a paste)."""
    f = sup
    submit(f)
    assert typed(f, 'supervisor') == [], 'delivery text was typed into the Codex input box'
    assert not [k for p, k in f.mgr.keys if p == f.agent('supervisor')['pane'] and k in ('Enter', 'C-m', 'Return')]
    assert len(queued(f)) == 1 and queued(f)[0]['text'] == body_of(f)
    assert codex_turn(f, plan(['worker-1']))['status'] == 'committed'


def test_manual_turn_with_delivery_only_in_history_is_not_settled(sup):
    """Operator types a turn; the delivery body is only in the cumulative history.
    Baseline: RED (committed through turn_answers' any())."""
    f = sup
    submit(f)
    body = body_of(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'just chatting')],
                     inputs=['startup prompt', body, 'just chatting'])
    assert out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'
    assert typed(f, 'worker-1') == []


def _two_same_head_reports(f):
    """worker-1 reports twice; both follow-ups share their first 120 characters."""
    submit(f)
    t0 = str(uuid.uuid4())
    assert codex_turn(f, plan(['worker-1']), turn=t0)['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format(SHARED + ' -- vendor A', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    first, first_users, t1 = body_of(f), None, str(uuid.uuid4())
    client, text, trigger = last_delivery(f)
    first_users, first_trigger = [(client, text)], trigger
    assert codex_turn(f, plan(['worker-1'], 'check vendor C too'), turn=t1)['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format(SHARED + ' -- vendor C', 'worker-1/c.md'))['status'] == 'committed'
    f.pump()
    second = body_of(f)
    head = lambda s: runtime_v2._norm(s)[:120]  # noqa: E731
    assert first != second and head(first) == head(second), (first[:160], second[:160])
    return first, second, t1, first_users, first_trigger


def test_late_duplicate_notify_of_an_old_turn_does_not_settle_a_same_head_delivery(sup):
    """The same turn-id notifies again with another payload while a newer delivery with the
    same 120-char head is in flight. Baseline: RED (it settles the newer delivery)."""
    f = sup
    first, second, t1, users, trigger = _two_same_head_reports(f)
    inputs = [x for x in history(f) if x != second]
    out = codex_turn(f, WAIT, turn=t1, users=users, trigger=trigger, inputs=inputs)
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received' and body_of(f) == second


def test_duplicate_notify_of_a_settled_turn_settles_nothing_else(sup):
    """Baseline: GREEN (guard)."""
    f = sup
    submit(f)
    turn = str(uuid.uuid4())
    client, text, trigger = last_delivery(f)
    assert codex_turn(f, plan(['worker-1']), turn=turn)['status'] == 'committed'
    f.pump()
    again = codex_turn(f, plan(['worker-2'], 'changed my mind'), turn=turn, users=[(client, text)], trigger=trigger)
    assert again is False or again['status'] == 'ignored', again
    f.pump()
    assert len(typed(f, 'worker-1')) == 1 and typed(f, 'worker-2') == []


def test_old_generation_notify_is_stale(sup):
    """Baseline: GREEN (guard)."""
    f = sup
    submit(f)
    assert codex_turn(f, plan(['worker-1']), generation=3)['status'] == 'stale'
    assert lease(f)['status'] == 'received'


def test_other_thread_notify_never_settles(sup):
    """Title side thread or foreign session. Baseline: GREEN (guard)."""
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), thread=str(uuid.uuid4()))
    assert out is False or out['status'] in ('ignored', 'stale', 'rejected'), out
    assert lease(f)['status'] == 'received' and typed(f, 'worker-1') == []


def test_notify_for_finished_turn_settles_even_if_next_turn_already_started(sup):
    """Fast callback through the real entry point. Baseline: RED (notify returns False)."""
    f = sup
    submit(f)
    turn = str(uuid.uuid4())
    nxt = turn_rows(str(uuid.uuid4()), 'user', [('', 'operator typed next')], '', complete=False)
    out = codex_turn(f, plan(['worker-1']), turn=turn, after=nxt)
    assert out is not False and out['status'] == 'committed', out


def test_notify_before_rollout_flush_settles_within_retry_window(sup):
    """Rollout written 0.5 s after the notify. Baseline: GREEN (guard)."""
    f = sup
    submit(f)
    out = codex_turn(f, plan(['worker-1']), late=.5)
    assert out is not False and out['status'] == 'committed', out


def test_busy_supervisor_gets_followups_one_at_a_time_and_each_once(sup):
    """Second report waits while the first is in flight, then goes once. Baseline: RED only on the
    typed assertion (queueing itself is the bus's job and holds)."""
    f = sup
    submit(f)
    assert codex_turn(f, plan(WORKERS))['status'] == 'committed'
    f.pump()
    assert claude_turn(f, 'worker-1', REPORT.format('wrote a', 'worker-1/a.md'))['status'] == 'committed'
    f.pump()
    n = len(delivered(f))
    assert claude_turn(f, 'worker-2', REPORT.format('wrote b', 'worker-2/b.md'))['status'] == 'committed'
    f.pump(); f.pump()
    assert len(delivered(f)) == n and f.r.llen(f.v2.bus.pending_key('supervisor')) == 1
    assert codex_turn(f, WAIT)['status'] == 'committed'
    f.pump(); f.pump()
    got = delivered(f)
    assert len(got) == n + 1 and got[-1].startswith('[worker-2 report]')
    assert typed(f, 'supervisor') == []


# ================================================================ C. fail closed, never fall back to paste
def test_failed_delivery_holds_and_is_never_retried_by_paste(sup, monkeypatch):
    """Channel failure (queue CLI error; baseline: dead pane) → hold, no success, no typing later.
    Baseline: GREEN for hold / no retry. The new tree must also type nothing at all."""
    f = sup
    pane = f.agent('supervisor')['pane']
    install_queue_stub(f, monkeypatch, fail='codex queue rc=1: simulated')
    if not HAS_QUEUE:
        f.mgr.alive.discard(pane)
    before = len(typed(f, 'supervisor', pane))
    submit(f)
    attempts = len(typed(f, 'supervisor', pane))
    assert f.agent('supervisor')['state'] == 'held' and f.paused()
    assert lease(f)['status'] == 'uncertain'
    assert not any(e.get('event') == 'tui.typed' and e.get('slot') == 'supervisor' for e in audit(f))
    for _ in range(3):
        f.pump(); f.v2.step(20); f.v2.renew_tui()
    assert len(typed(f, 'supervisor', pane)) == attempts
    if HAS_QUEUE:
        assert attempts == before  # no paste attempt at all once the queue channel exists


def test_v2_send_refuses_paste_fallback(sup):
    """Baseline: GREEN (guard)."""
    f = sup
    with pytest.raises(ValueError, match='fallback forbidden'):
        f.send('supervisor', f.agent('supervisor')['pane'], 'anything')


# ================================================================ D. queue binding (plan §2 rules, §3.1)
needs_queue = pytest.mark.skipif(not HAS_QUEUE, reason='cmux_fleet.codex_queue not in this tree (plan §3.1)')


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


@needs_queue
def test_bound_accepts_only_the_queue_turn_that_carries_the_delivery(tmp_path):
    from cmux_fleet import codex_queue as cq
    now = int(time.time() * 1000)
    thread, sessions = _sessions(tmp_path, turn_rows('TQ', 'queue', [('client-1', 'DELIVERY')], 'OK', now + 100))
    rec = _rec(thread, 'DELIVERY', 'client-1', now)
    assert cq.bound(rec, thread, 'TQ', sessions) == (True, '')


@needs_queue
def test_race_idle_check_then_user_turn_absorbs_the_delivery_is_rejected(tmp_path):
    """Race the supervisor named: the thread looked idle, the operator started an unknown turn,
    and the delivery got merged into that turn. The turn-id is new (not in any 'known turn-id'
    set), started after the delivery was queued, and even carries the delivery's own client_id.
    It must still not settle (rule 2: trigger is not "queue")."""
    from cmux_fleet import codex_queue as cq
    now = int(time.time() * 1000)
    rows = turn_rows('TU', 'user', [('', 'operator question'), ('client-1', 'DELIVERY')], 'answer to operator', now + 50)
    thread, sessions = _sessions(tmp_path, rows)
    ok, why = cq.bound(_rec(thread, 'DELIVERY', 'client-1', now), thread, 'TU', sessions)
    assert ok is False, why


@needs_queue
def test_race_operator_input_merged_into_the_queue_turn_is_rejected(tmp_path):
    """Reverse merge (measured in the TUI: input typed during a running turn joins that turn).
    The queue turn then has two user messages; whatever it answered is not provably the delivery."""
    from cmux_fleet import codex_queue as cq
    now = int(time.time() * 1000)
    rows = turn_rows('TQ', 'queue', [('client-1', 'DELIVERY'), ('', 'operator steer')], 'mixed answer', now + 50)
    thread, sessions = _sessions(tmp_path, rows)
    ok, why = cq.bound(_rec(thread, 'DELIVERY', 'client-1', now), thread, 'TQ', sessions)
    assert ok is False, why


@needs_queue
def test_queued_item_id_is_not_a_turn_id(tmp_path):
    """`codex queue` prints a queued item id. A notify that carries that id as turn-id
    (or any turn missing from the rollout) never binds."""
    from cmux_fleet import codex_queue as cq
    now = int(time.time() * 1000)
    thread, sessions = _sessions(tmp_path, turn_rows('TQ', 'queue', [('client-1', 'DELIVERY')], 'OK', now + 50))
    rec = _rec(thread, 'DELIVERY', 'client-1', now)
    ok, why = cq.bound(rec, thread, rec['cq_queued_id'], sessions)
    assert ok is False, why


@needs_queue
def test_other_client_id_with_identical_text_is_rejected(tmp_path):
    """A replayed delivery with the same text: the older queue turn (other client_id) is not this delivery."""
    from cmux_fleet import codex_queue as cq
    now = int(time.time() * 1000)
    thread, sessions = _sessions(tmp_path, turn_rows('TOLD', 'queue', [('client-old', 'DELIVERY')], 'OK', now + 10))
    ok, why = cq.bound(_rec(thread, 'DELIVERY', 'client-new', now), thread, 'TOLD', sessions)
    assert ok is False, why


@needs_queue
def test_identical_text_old_queue_turn_within_skew_without_client_id_is_rejected(tmp_path):
    """client_id not captured. Measured (worker-2 lab/evidence4/cq_client_id_probe.jsonl): against an
    idle daemon-mode TUI the queued row was already consumed when `codex queue` returned, 6 of 6
    times, so an empty cq_client_id is the normal case, not an edge case. The same text was
    delivered before (replay / resume). The older queue turn
    started 1 s before this delivery was queued, inside START_SKEW_MS. Without the unique id,
    text plus time cannot tell the two deliveries apart, so binding must not succeed."""
    from cmux_fleet import codex_queue as cq
    now = int(time.time() * 1000)
    thread, sessions = _sessions(tmp_path, turn_rows('TOLD', 'queue', [('client-old', 'DELIVERY')], 'OK', now - 1000))
    ok, why = cq.bound(_rec(thread, 'DELIVERY', '', now), thread, 'TOLD', sessions)
    assert ok is False, why


@needs_queue
def test_bound_rejects_other_thread_edited_text_old_turn_and_rebinding(tmp_path):
    from cmux_fleet import codex_queue as cq
    now = int(time.time() * 1000)
    rows = turn_rows('TQ', 'queue', [('client-1', 'DELIVERY edited by operator')], 'OK', now + 50) + \
        turn_rows('TOLD', 'queue', [('client-1', 'DELIVERY')], 'OK', now - 60000)
    thread, sessions = _sessions(tmp_path, rows)
    rec = _rec(thread, 'DELIVERY', 'client-1', now)
    assert cq.bound(rec, str(uuid.uuid4()), 'TQ', sessions)[0] is False          # rule 1
    assert cq.bound(rec, thread, 'TQ', sessions)[0] is False                     # rule 3 (text edited)
    assert cq.bound(rec, thread, 'TOLD', sessions)[0] is False                   # rule 5 (older turn)
    assert cq.bound(dict(rec, cq_turn='TX', cq_state='bound'), thread, 'TQ', sessions)[0] is False  # rule 6
    assert cq.bound(dict(rec, cq_state='withdrawn'), thread, 'TQ', sessions)[0] is False
    assert cq.bound(dict(rec, cq_state='failed'), thread, 'TQ', sessions)[0] is False


@needs_queue
def test_runtime_user_turn_absorbing_the_delivery_is_not_settled(sup):
    """End-to-end form of the merge race through fleet.notify: a user-triggered turn whose
    inputs contain the delivery (its own client_id, last position) must not settle."""
    f = sup
    submit(f)
    client, text, _ = last_delivery(f)
    out = codex_turn(f, plan(['worker-1']), trigger='user', users=[('', 'operator question'), (client, text)])
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received' and typed(f, 'worker-1') == []


@needs_queue
def test_runtime_turn_id_equal_to_queued_item_id_is_not_settled(sup):
    """The rollout has the real queue turn for the delivery; the notify names the queued item id
    as its turn-id. That id is not a turn, so nothing settles."""
    f = sup
    submit(f)
    qid = f.test_queue[-1]['queued_id']
    client, text, _ = last_delivery(f)
    thread, answer = f.test_threads['supervisor'], plan(['worker-1'])
    write_rollout(f.test_sessions, thread, fleet.agent_cwd(f.cfg, 'supervisor'),
                  rows=turn_rows(str(uuid.uuid4()), 'queue', [(client, text)], answer))
    payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': qid,
                          'input-messages': history(f), 'last-assistant-message': answer})
    out = drive(f, lambda: fleet.notify(f.path, 'supervisor', '0', payload, f.test_sessions))
    assert out is False or out['status'] == 'ignored', out
    assert lease(f)['status'] == 'received'
