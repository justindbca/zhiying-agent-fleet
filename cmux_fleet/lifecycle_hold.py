"""Lifecycle hold and service idling (lifecycle design §2 row 1, §3):

* ``blocked`` / ``budget_exhausted``: the run stays exactly as it is (tabs, sessions, Redis) for
  ``on_<status>.hold_hours`` (default 12) counted from result.json ``at``, then ``then:
  stop_keep_resumable``: every runtime is retired, Redis is shut down with its AOF directory kept, and
  result.json says ``resumable: true`` so ``start --resume`` reopens the task (docs/design.md §17).
  The values are re-read every tick, so an edit of hold_hours applies on the next tick.
* service: a worker idle for ``worker_idle_reclaim_min`` minutes is retired (the supervisor stays);
  work queued for it later starts its next generation. ``idle_stop_hours`` (null = never) stops the
  whole run after that long without requests or busy agents.
"""
import time


def hold_tick(f, policy, task, now):
    from cmux_fleet import lifecycle_close as close
    from cmux_fleet.fleet import load
    status, tid = task.get('status'), task['id']
    rule = policy.get('on_' + status) or {}
    try:
        since = float(load(f.run_dir / 'result.json')['at'])
    except (OSError, ValueError, KeyError, TypeError):
        since = now
    hours = rule.get('hold_hours', 12)
    if not close._hget(f, 'held:' + tid):
        close._hset(f, **{'held:' + tid: now})
        f.log('lifecycle.held', status=status, hold_hours=hours)
        close.notify(f, 'held', '[fleet ' + close.fleet_name(f) + '] ' + policy['name'] + ' ' + tid + ' ' + status
                     + '：' + task.get('reason', '') + '。保留现场 ' + str(hours) + ' 小时等人处理，之后自动停机并保留可续跑。'
                     + ' run: ' + f.cfg['run'], policy)
    if hours is None or now - since < float(hours) * 3600:
        return
    then = rule.get('then', 'stop_keep_resumable')
    if then != 'stop_keep_resumable':
        f.log('lifecycle.table_error', error='on_' + status + '.then ' + repr(then) + ' is not supported; holding')
        return
    close.request_close(f, 'timeout', status + ' held ' + str(hours) + ' h without a human; stopped, resumable',
                        resumable=True)


def _lease_idle(f, slot):
    return f.v2.bus.r.hget(f.v2.bus.lease_key(slot), 'status') in (None, 'idle')


def service_tick(f, policy, now):
    from cmux_fleet import lifecycle_close as close
    from cmux_fleet.fleet import REQQ
    minutes = policy.get('worker_idle_reclaim_min')
    paused = f.paused()
    for slot in f.workers:
        info = f.agent(slot)
        pending = f.v2.bus.pending(slot)
        if info.get('state') == 'reclaimed':
            if pending and not paused:  # work arrived for a reclaimed worker: its next generation starts
                f.log('lifecycle.respawn', slot=slot, pending=len(pending))
                f.v2.respawn([slot], True)
            continue
        if (minutes is None or paused or info.get('state') != 'idle' or not info.get('pane') or pending
                or not _lease_idle(f, slot) or now - f.last_activity(info) < float(minutes) * 60):
            continue
        if f.v2.retire(slot, 'idle for ' + str(minutes) + ' min (worker_idle_reclaim_min)'):
            f.setf(slot, state='reclaimed', pane='', reclaimed_at=now)
            f.log('lifecycle.reclaimed', slot=slot, idle_min=minutes)
    hours = policy.get('idle_stop_hours')
    active = f.r.llen(REQQ) or any(f.agent(s).get('state') in ('busy', 'starting', 'handoff') for s in f.names)
    last = close._hget(f, 'service_active_at')
    if active or last is None:
        close._hset(f, service_active_at=now)
        return
    if hours is not None and now - float(last) >= float(hours) * 3600:
        close.request_close(f, 'timeout', 'service idle for ' + str(hours) + ' h (idle_stop_hours)')


def reopen(r, task, extend_hours=None, now=None):
    """start --resume of a ``resumable`` run: the held task runs again. A task stopped for its time budget
    needs ``--extend-hours`` (its deadline already passed: §17 would end it again on the first tick)."""
    from cmux_fleet.fleet import TASK
    now = time.time() if now is None else now
    fields = {'status': 'running', 'reason': ''}
    deadline = float(task.get('deadline') or 0)
    if extend_hours:
        fields['deadline'] = max(deadline, now) + float(extend_hours) * 3600
    elif deadline <= now:
        raise ValueError('task ' + task.get('id', '') + ' is past its time budget; pass --extend-hours H to resume it')
    r.hset(TASK, mapping=fields)
    from cmux_fleet.lifecycle_close import LIFE
    r.hdel(LIFE, *['held:' + task.get('id', ''), 'gate:' + task.get('id', ''), 'timeout:' + task.get('id', '')])
    return fields
