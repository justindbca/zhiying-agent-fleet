"""Codex deliveries through `codex queue`: no paste, no Enter, and an exact turn binding.

The running Codex TUI takes queued messages from $CODEX_HOME/queue_1.sqlite, one turn per
item in FIFO order, also while it is busy (a queued item is never merged into a running turn).
The CLI prints the queued item id; the item's payload carries a client_id that the rollout
records on the user message of the turn it started, together with turn_trigger "queue".

A delivery is recorded on the agent hash *before* the CLI runs (cq_* fields), so a turn end
that is reported faster than the CLI returns still finds it. A turn settles a delivery only
if the rollout proves it: bound thread, a queue-triggered turn, exactly one user message whose
text is the delivered text (and whose client_id is the queued one when known), started after
the delivery was queued. The cumulative notify `input-messages` list is not used. Any failure
leaves the slot to the existing fail-closed paths; nothing falls back to typing.
"""
import datetime
import json
import os
import re
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path

QUEUE_DB = 'queue_1.sqlite'
QUEUE_BIND_WAIT = 10.0  # seconds a turn end waits for a delivery still being queued
START_SKEW_MS = 2000  # with a client_id the id decides; the time check only rejects clearly older turns
CLOCK_SKEW_MS = 50  # without a client_id: same-host clocks, ms rounding only
FIELDS = ('cq_delivery', 'cq_text', 'cq_thread', 'cq_queued_at', 'cq_state', 'cq_queued_id',
          'cq_client_id', 'cq_turn')
QUEUED = re.compile(r'Queued message (\S+) for thread (\S+?)\.?\s*$', re.M)


class QueueError(RuntimeError):
    pass


def _fleet():
    from cmux_fleet import fleet
    return fleet


def is_codex(cfg, name):
    return _fleet().agent_cli(cfg, name) == 'codex'


def db_path(cfg):
    return _fleet().codex_home(cfg) / QUEUE_DB


def queue_argv(cfg, thread, text):
    return [cfg.get('codex') or 'codex', 'queue', '--thread', thread, '--message', text]


def queued_rows(cfg, thread=None):
    """Items still waiting in the queue (read-only). A consumed item is gone from the table."""
    path = db_path(cfg)
    if not path.exists():
        return []
    con = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True, timeout=5)
    try:
        sql, args = 'SELECT id, thread_id, payload_json, queue_order FROM queued_items', ()
        if thread:
            sql, args = sql + ' WHERE thread_id = ?', (thread,)
        rows = con.execute(sql + ' ORDER BY queue_order', args).fetchall()
    finally:
        con.close()
    out = []
    for qid, tid, payload, order in rows:
        try:
            item = json.loads(payload).get('UserInput') or {}
        except ValueError:
            item = {}
        text = '\n'.join(c.get('text', '') for c in item.get('content') or [] if isinstance(c, dict) and c.get('type') == 'text')
        out.append({'id': qid, 'thread_id': tid, 'client_id': item.get('client_id') or '', 'text': text,
                    'queue_order': order})
    return out


def withdraw(cfg, thread, ids):
    """Remove not-yet-started items; a removed item never runs. Returns the number removed."""
    ids = [i for i in ids if i]
    path = db_path(cfg)
    if not ids or not thread or not path.exists():
        return 0
    con = sqlite3.connect(str(path), timeout=5)
    try:
        n = con.execute('DELETE FROM queued_items WHERE thread_id = ? AND id IN (%s)' % ','.join('?' * len(ids)),
                        (thread, *ids)).rowcount
        con.commit()
    finally:
        con.close()
    return n


def enqueue(cfg, name, thread, text, *, run=subprocess.run, timeout=30):
    """Queue one message for the session `thread`; {'queued_id', 'client_id', 'thread'}."""
    fleet = _fleet()
    if not thread or not re.fullmatch(fleet.THREAD_RE, thread):
        raise QueueError('no bound Codex session to queue into')
    env = dict(os.environ, CODEX_HOME=str(fleet.codex_home(cfg)))
    try:
        p = run(queue_argv(cfg, thread, text), env=env, cwd=str(fleet.agent_cwd(cfg, name)),
                capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        # Unknown outcome (e.g. timeout after the insert): withdraw a matching pending item so it cannot run unbound.
        try:
            withdraw(cfg, thread, [row['id'] for row in queued_rows(cfg, thread) if row['text'] == text])
        except sqlite3.Error:
            pass
        raise QueueError('codex queue failed: ' + str(exc)[:300])
    m = QUEUED.search(p.stdout or '')
    if p.returncode != 0 or not m:
        raise QueueError('codex queue rc=' + str(p.returncode) + ': ' + ((p.stderr or '') + (p.stdout or '')).strip()[-300:])
    if m.group(2) != thread:
        raise QueueError('codex queue answered for another thread: ' + m.group(2))
    client = ''
    try:
        client = next((row['client_id'] for row in queued_rows(cfg, thread) if row['id'] == m.group(1)), '')
    except sqlite3.Error:
        pass  # binding rules 1-3 and 5 still hold without it
    return {'queued_id': m.group(1), 'client_id': client, 'thread': thread}


def _ms(stamp):
    try:
        return int(datetime.datetime.fromisoformat(str(stamp).replace('Z', '+00:00')).timestamp() * 1000)
    except ValueError:
        return 0


def turn_record(thread, turn, sessions):
    """What the rollout says about one turn: {'trigger', 'started_ms', 'users': [{'client_id', 'text'}]}."""
    fleet = _fleet()
    for path in fleet.rollouts(thread, sessions):
        try:
            if fleet._is_subagent(fleet._meta(path)):
                continue
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except (OSError, ValueError):
            continue
        found, users = None, []
        for line in lines:
            if turn not in line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get('payload') or {}
            if row.get('type') != 'event_msg':
                continue
            if payload.get('type') == 'task_started':
                attribution = payload.get('turn_attribution') or {}
                if (attribution.get('turn_id') or payload.get('turn_id')) == turn:
                    found = {'trigger': attribution.get('turn_trigger') or '', 'started_ms': _ms(row.get('timestamp'))}
            elif payload.get('type') == 'item_completed' and payload.get('turn_id') == turn:
                item = payload.get('item') or {}
                if item.get('type') == 'UserMessage':
                    users.append({'client_id': item.get('client_id') or '', 'text': '\n'.join(
                        c.get('text', '') for c in item.get('content') or []
                        if isinstance(c, dict) and c.get('type') == 'text')})
        if found:
            return dict(found, users=users)
    return None


def record(info):
    return {k: info.get(k, '') for k in FIELDS}


def bound(rec, thread, turn, sessions):
    """(True, '') only if the rollout proves `turn` was started by the delivery in `rec`."""
    if not rec.get('cq_delivery') or rec.get('cq_state') not in ('queued', 'bound'):
        return False, 'no queued delivery (state ' + (rec.get('cq_state') or 'none') + ')'
    if thread != rec.get('cq_thread'):
        return False, 'turn belongs to another thread'
    if rec.get('cq_turn') and rec['cq_turn'] != turn:
        return False, 'delivery already bound to turn ' + rec['cq_turn']
    if not turn:
        return False, 'turn end carries no turn id'
    facts = turn_record(thread, turn, sessions)
    if not facts:
        return False, 'turn not found in the rollout'
    if facts['trigger'] != 'queue':
        return False, 'turn was not started by the queue (trigger ' + (facts['trigger'] or 'none') + ')'
    if len(facts['users']) != 1:
        return False, 'turn has ' + str(len(facts['users'])) + ' user messages'
    user = facts['users'][0]
    if user['text'] != rec.get('cq_text'):
        return False, 'turn input is not the delivered text'
    queued_at = int(float(rec.get('cq_queued_at') or 0))
    if rec.get('cq_client_id'):
        if user['client_id'] != rec['cq_client_id']:
            return False, 'turn input has another client_id'
        if facts['started_ms'] and facts['started_ms'] < queued_at - START_SKEW_MS:
            return False, 'turn started before the delivery was queued'
        return True, ''
    # No client_id (a daemon-mode TUI often consumes the item before the CLI returns): the turn must
    # start after the record was written (same host clock) and be the first queue turn with this text.
    if not facts['started_ms'] or facts['started_ms'] < queued_at - CLOCK_SKEW_MS:
        return False, 'turn started before the delivery was queued (no client_id to tell them apart)'
    first = first_queue_turn(thread, rec.get('cq_text'), queued_at - CLOCK_SKEW_MS, sessions)
    if first != turn:
        return False, 'an earlier queue turn (' + str(first) + ') carries the same text'
    return True, ''


def first_queue_turn(thread, text, since_ms, sessions):
    """Id of the earliest queue-triggered turn started at/after since_ms whose only input is `text`."""
    fleet = _fleet()
    for path in fleet.rollouts(thread, sessions):
        try:
            if fleet._is_subagent(fleet._meta(path)):
                continue
            lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        except (OSError, ValueError):
            continue
        started, users = {}, {}
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            payload = row.get('payload') or {}
            if row.get('type') != 'event_msg':
                continue
            if payload.get('type') == 'task_started':
                attribution = payload.get('turn_attribution') or {}
                tid = attribution.get('turn_id') or payload.get('turn_id')
                if attribution.get('turn_trigger') == 'queue' and _ms(row.get('timestamp')) >= since_ms:
                    started[tid] = _ms(row.get('timestamp'))
            elif payload.get('type') == 'item_completed' and (payload.get('item') or {}).get('type') == 'UserMessage':
                users.setdefault(payload.get('turn_id'), []).append('\n'.join(
                    c.get('text', '') for c in payload['item'].get('content') or []
                    if isinstance(c, dict) and c.get('type') == 'text'))
        hits = sorted((ms, tid) for tid, ms in started.items() if users.get(tid) == [text])
        if hits:
            return hits[0][1]
    return None


def _real(r):
    """The real Redis client behind runtime_v2's lazy Effects overlay: the record must be durable
    before the CLI runs, not deferred to the end of a transaction."""
    return getattr(r, 'source', r)


def deliver(f, slot, delivery, text, thread=None):
    """Queue `text` for the slot's bound session (or `thread`, e.g. an adopted pane's session not yet
    bound to the new slot). Raises QueueError (state 'failed') on any failure."""
    fleet = _fleet()
    r = _real(f.r)
    info = r.hgetall(fleet.key('agent', slot))
    thread = thread or info.get('thread') or ''
    pane = info.get('pane') or ''
    if not pane or f.mgr.get_pane_status(pane) is None:
        # A queued item outlives its TUI and runs whenever the session is resumed later; never queue blind.
        r.hset(fleet.key('agent', slot), mapping={'cq_state': 'failed'})
        f.log('queue.failed', agent=slot, delivery=delivery, error='runtime pane is gone')
        raise QueueError('runtime pane is gone; not queued')
    old = record(info)
    if old.get('cq_state') in ('queuing', 'queued') and old.get('cq_queued_id'):
        clear(f, slot, 'superseded by ' + delivery)
    r.hset(fleet.key('agent', slot), mapping={'cq_delivery': delivery, 'cq_text': text, 'cq_thread': thread,
                                              'cq_queued_at': str(int(time.time() * 1000)), 'cq_state': 'queuing',
                                              'cq_queued_id': '', 'cq_client_id': '', 'cq_turn': ''})
    try:
        got = enqueue(f.runtime_cfg(slot), slot, thread, text)
    except QueueError as exc:
        r.hset(fleet.key('agent', slot), mapping={'cq_state': 'failed'})
        f.log('queue.failed', agent=slot, delivery=delivery, error=str(exc)[:300])
        raise
    r.hset(fleet.key('agent', slot), mapping={'cq_state': 'queued', 'cq_queued_id': got['queued_id'],
                                              'cq_client_id': got['client_id']})
    f.log('queue.delivered', agent=slot, delivery=delivery, queued_id=got['queued_id'],
          client_id=got['client_id'] or 'missing', thread=thread)
    return got


def clear(f, slot, reason):
    """Withdraw the slot's pending queued item (if not started yet); the record becomes 'withdrawn'."""
    fleet = _fleet()
    r = _real(f.r)
    rec = record(r.hgetall(fleet.key('agent', slot)))
    if rec['cq_state'] not in ('queuing', 'queued'):
        return 0
    try:
        n = withdraw(f.runtime_cfg(slot), rec['cq_thread'], [rec['cq_queued_id']])
    except sqlite3.Error as exc:
        f.log('queue.withdraw_failed', agent=slot, error=str(exc)[:300])
        n = 0
    r.hset(fleet.key('agent', slot), mapping={'cq_state': 'withdrawn'})
    f.log('queue.withdrawn', agent=slot, delivery=rec['cq_delivery'], removed=n, reason=reason)
    return n


def mark_bound(r, slot, turn):
    _real(r).hset(_fleet().key('agent', slot), mapping={'cq_state': 'bound', 'cq_turn': turn})


def wait_queued(r, slot, timeout=QUEUE_BIND_WAIT):
    """Fast callback: let a delivery still in 'queuing' reach 'queued' (or fail) before binding."""
    fleet = _fleet()
    end = time.monotonic() + timeout
    while True:
        rec = record(_real(r).hgetall(fleet.key('agent', slot)))
        if rec['cq_state'] != 'queuing' or time.monotonic() >= end:
            return rec
        time.sleep(.1)


def new_delivery_id(prefix):
    return prefix + '-' + uuid.uuid4().hex[:12]
