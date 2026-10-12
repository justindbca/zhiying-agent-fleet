"""Transport integration for the existing Fleet dispatcher, not another scheduler.

Daemon effects are first evaluated against a lazy Redis overlay and saved as an
immutable intent. A preflighted Redis script applies the effects, public/UI results,
PEL completion and event ACK together. Prepared intents are replayable. An unexpected
script failure with an 'applying' intent is ambiguous and holds the run for inspection;
Lua does not roll back failed writes. No model/tool side effect is ever replayed.
"""
import copy
import hashlib
import json
import time
import uuid
from pathlib import Path

import redis

from cmux_fleet.message_bus import BusError, MessageBus, _json, _short, _request_id
from cmux_fleet.presentation import mask_secrets, result_card, routing_card, sanitize_text


def validate_config(cfg):
    version = cfg.get('transport_version', 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('transport_version must be 1 or 2')
    if version == 2:
        if (cfg.get('supervisor_cli', 'codex') != 'pi'
                or cfg.get('worker_cli', 'codex') != 'pi'):
            raise ValueError('transport v2 currently supports all-Pi fleets only; Claude/Codex capabilities unvalidated')
        if cfg.get('allow_development_v2') is not True:
            raise ValueError('transport v2 requires --allow-development-v2; real SDK/TUI acceptance is pending')
    return version


def bus_for(cfg, r):
    return MessageBus(r, cfg.get('run_id') or Path(cfg['run']).name,
                      lease_ms=int(cfg.get('v2_lease_ms', 300000)))


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


# Stable producer dedup is run-local, never expires while retries are possible.
LUA_EVENT = """
local old = redis.call('GET', KEYS[1])
if old then
  if old ~= ARGV[1] then error('event_conflict') end
  return 'exists'
end
local typ = redis.call('TYPE', KEYS[2]).ok
if typ ~= 'none' and typ ~= 'stream' then error('event_wrong_type') end
redis.call('XADD', KEYS[2], '*', 'event_id', ARGV[2], 'data', ARGV[1])
redis.call('SET', KEYS[1], ARGV[1])
return 'added'
"""


def post_event(bus, event, event_id=None):
    if not isinstance(event, dict) or not isinstance(event.get('kind'), str):
        raise ValueError('event requires an object with a kind')
    event_id = _short(event_id or 'e-' + uuid.uuid4().hex, 'event_id')
    raw = dumps(event)
    if len(raw.encode()) > 1000000:
        raise ValueError('event exceeds 1000000 bytes')
    bus.r.eval(LUA_EVENT, 2, bus.prefix + ':producer:' + event_id,
               bus.prefix + ':events', raw, event_id)
    return event_id


def wait_reply(bus, correlation, timeout=30, reader=None):
    _request_id(correlation)
    own = reader is None
    reader = reader if reader is not None else bus.blocking_client(1000)
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            rows = reader.xread({bus.prefix + ':reply:' + correlation: '0-0'}, count=1, block=1000)
            if rows:
                return json.loads(rows[0][1][0][1]['data'])
        return {'status': 'pending', 'request_id': correlation}
    finally:
        if own:
            reader.close()
            reader.connection_pool.disconnect()


class Effects:
    """Lazy, single-daemon read-your-writes overlay for Fleet's existing validators.

    Only scheduler-owned string/hash/list keys may be accessed here. The bridge
    writes events, lease renewal and UI cursors, not these scheduler keys. No real
    mutation occurs before the complete effect plan has been persisted.
    """
    def __init__(self, r):
        self.source, self.values, self.commands = r, {}, []

    def value(self, key, kind):
        if key not in self.values:
            actual = self.source.type(key)
            if actual not in ('none', kind):
                raise BusError('wrong_type:' + key)
            self.values[key] = (kind, {'hash': self.source.hgetall,
                'list': lambda k: self.source.lrange(k, 0, -1), 'string': self.source.get}[kind](key))
        if self.values[key][0] != kind:
            raise BusError('overlay wrong_type:' + key)
        return self.values[key][1]

    def command(self, *args):
        self.commands.append([str(a) for a in args])

    def get(self, key):
        return self.value(key, 'string')

    def set(self, key, value, ex=None, nx=False):
        if nx and self.get(key) is not None:
            return False
        self.values[key] = ('string', str(value))
        self.command('SET', key, value, *(['EX', ex] if ex else []))
        return True

    def hgetall(self, key):
        return dict(self.value(key, 'hash'))

    def hget(self, key, field):
        return self.value(key, 'hash').get(field)

    def hset(self, key, mapping):
        self.value(key, 'hash').update({k: str(v) for k, v in mapping.items()})
        self.command('HSET', key, *[x for k, v in mapping.items() for x in (k, v)])

    def hincrby(self, key, field, n):
        value = int(self.hget(key, field) or 0) + n
        self.hset(key, {field: value})
        return value

    def delete(self, *keys):
        for key in keys:
            self.values.pop(key, None)
            # Subsequent reads of this key must see absence, not the source value.
            actual = self.source.type(key)
            if actual in ('string', 'hash', 'list'):
                self.values[key] = (actual, {'string': None, 'hash': {}, 'list': []}[actual])
        self.command('DEL', *keys)

    def rpush(self, key, value):
        self.value(key, 'list').append(str(value)); self.command('RPUSH', key, value)

    def lpush(self, key, value):
        self.value(key, 'list').insert(0, str(value)); self.command('LPUSH', key, value)

    def lrange(self, key, a, b):
        items = self.value(key, 'list')
        return items[a:] if b == -1 else items[a:b + 1]

    def llen(self, key):
        return len(self.value(key, 'list'))

    def lrem(self, key, count, value):
        if count != 0:
            raise ValueError('effect LREM only supports count=0')
        self.values[key] = ('list', [x for x in self.value(key, 'list') if x != value])
        self.command('LREM', key, count, value)

    def ltrim(self, key, a, b):
        self.values[key] = ('list', self.lrange(key, a, b)); self.command('LTRIM', key, a, b)

    def xadd(self, key, fields):
        self.command('XADD', key, '*', *[x for k, v in fields.items() for x in (k, v)])


# Preflight all expected key types before any effects. Applying marker is written
# separately; unanticipated Redis script failure leaves a visible ambiguous intent.
LUA_APPLY = """
if redis.call('HGET', KEYS[1], 'phase') == 'applied' then return 'already' end
if redis.call('HGET', KEYS[1], 'phase') ~= 'applying' then error('intent_not_applying') end
local plan = cjson.decode(ARGV[1])
for _, cmd in ipairs(plan.commands) do
  local want = ({HSET='hash', RPUSH='list', LPUSH='list', LREM='list',
    LTRIM='list', XADD='stream', XACK='stream', SET='string'})[cmd[1]]
  if cmd[1] ~= 'DEL' and not want then error('unsupported_effect') end
  if want then
    local got = redis.call('TYPE', cmd[2]).ok
    if got ~= 'none' and got ~= want then error('effect_wrong_type:' .. cmd[2]) end
  end
  if cmd[1] == 'XACK' then
    local found = false
    for _, group in ipairs(redis.call('XINFO', 'GROUPS', cmd[2])) do
      for i=1,#group,2 do
        if group[i] == 'name' and group[i+1] == cmd[3] then found = true end
      end
    end
    if not found then error('effect_missing_group') end
  end
end
-- The immutable authorization is rechecked at application, including on replay.
if type(plan.authorization) == 'table' then
  for key, fields in pairs(plan.authorization) do
    for field, expected in pairs(fields) do
      if redis.call('HGET', key, field) ~= expected then error('intent_identity:' .. field) end
    end
  end
end
if type(plan.commit_gate) == 'table' then
  local gate = plan.commit_gate
  local t = redis.call('TIME')
  local at = tonumber(t[1])*1000 + math.floor(tonumber(t[2])/1000)
  local deadline = tonumber(redis.call('HGET', gate.message, 'deadline_at'))
  local until_ = tonumber(redis.call('HGET', gate.lease, 'lease_until'))
  if not deadline or not until_ or at >= deadline or at >= until_ then
    error('settlement_expired_before_apply')
  end
  local pending = redis.call('XPENDING', gate.inbox, gate.group, gate.stream_id, gate.stream_id, 1)
  if #pending ~= 1 or pending[1][2] ~= gate.consumer then error('settlement_not_pending') end
end
for _, cmd in ipairs(plan.commands) do redis.call(unpack(cmd)) end
redis.call('HSET', KEYS[1], 'phase', 'applied')
return 'applied'
"""


class RuntimeV2:
    def __init__(self, fleet):
        self.f = fleet
        self.bus = bus_for(fleet.cfg, fleet.r)
        self.prefix = self.bus.prefix
        self.events = self.prefix + ':events'
        self.group = 'daemon'
        self.consumer = 'daemon'  # fixed same-run identity, exclusive daemon.lock
        self.reader = self.bus.blocking_client(1000)
        self.effects = None
        self.context = None
        self.queue_index = 0
        self.rotation_pending = set()
        self.pending_cursor = '0-0'
        self.held_events = set()
        try:
            fleet.r.xgroup_create(self.events, self.group, '0-0', mkstream=True)
        except redis.ResponseError as exc:
            if 'BUSYGROUP' not in str(exc):
                raise

    def close(self):
        self.reader.close(); self.reader.connection_pool.disconnect()

    def audit(self, event, **fields):
        data = mask_secrets(fields)
        fields = {k: sanitize_text(v) if isinstance(v, str) else dumps(v) for k, v in data.items()}
        fields.update(event=event, run_id=self.bus.run_id, at=str(int(time.time()*1000)))
        if self.effects:
            self.effects.xadd(self.bus.audit_key, fields)
        else:
            self.bus.r.xadd(self.bus.audit_key, fields)

    def log(self, kind, data):
        self.audit(kind, **data)

    def ui(self, slot, card):
        fields = {'event_id': card['metadata']['event_id'], 'data': dumps(card)}
        if self.effects:
            self.effects.xadd(self.prefix + ':ui:' + slot, fields)
        else:
            self.bus.r.xadd(self.prefix + ':ui:' + slot, fields)

    def depth(self, slot):
        r = self.effects or self.bus.r
        return r.llen(self.bus.pending_key(slot))

    def pending_text(self, slot):
        r = self.effects or self.bus.r
        mids = r.lrange(self.bus.pending_key(slot), 0, 0)
        return [json.loads(r.hget(self.bus.msg_key(mids[0]), 'body'))['text']] if mids else []

    def queue(self, slot, text, *, request_id=None, kind=None, deadline_at=None):
        from cmux_fleet.fleet import REQ_HEAD, SUPERVISOR_INSTRUCTION_HEAD, REQ
        if self.effects is None:
            return self.transaction('q-' + uuid.uuid4().hex, lambda: self.queue(
                slot, text, request_id=request_id, kind=kind, deadline_at=deadline_at))
        # These headers are generated by the original dispatcher, not parsed from
        # arbitrary assistant text to infer execution authorization.
        head = REQ_HEAD.match(text)
        task_head = SUPERVISOR_INSTRUCTION_HEAD.search(text)
        if head:
            request_id = head.group(1)
            text = text[head.end():].lstrip()
            req = self.f.r.hgetall(REQ + request_id)
            if req:
                spec = json.loads(req['spec'])
                deadline_at = int((float(req['created']) + float(spec.get('timeout_s')
                    or self.f.cfg['request_timeout'])) * 1000)
        elif text.startswith('[New task '):
            end = text.index(']')
            request_id = text[len('[New task '):end]
            text = text[end+1:].lstrip()
        elif slot != 'supervisor' and task_head:
            request_id = task_head.group(1)
            text = text[:task_head.start()] + text[task_head.end():]
        request_id = request_id or (self.context or {}).get('bound_request') or self.f.task().get('id') or 'notice'
        if not self.f.service and self.f.task().get('deadline'):
            deadline_at = deadline_at or int(float(self.f.task()['deadline'])*1000)
        deadline_at = deadline_at or int(time.time()*1000) + 3600000
        kind = kind or ('supervisor' if slot == 'supervisor' else 'worker')
        # Prompt instructions remain business content. Transport labels are metadata.
        text = text.replace('Reply with the service decision JSON from AGENTS.md.', 'Use fleet_decision to route this request.')
        text = text.replace('Reply with the JSON format from AGENTS.md.', 'Use fleet_decision when you have decided.')
        text = text.replace('Continue the current task using the AGENTS.md decision JSON.', 'Continue the current task using fleet_decision.')
        self.queue_index += 1
        mid = 'm-' + digest(self.context['intent_id'] + ':' + str(self.queue_index))[:32]
        body = {'text': text, 'title': '派工任务' if slot != 'supervisor' else '待处理业务',
                'mode': self.f.cfg['mode'], 'purpose': kind}
        if self.f.service and head and slot != 'supervisor':
            body['reply'] = json.loads(self.f.r.hget(REQ + request_id, 'spec'))['reply']
        self.effects.hset(self.bus.msg_key(mid), dict(run_id=self.bus.run_id, slot=slot,
            message_id=mid, body=_json(body), request_id=request_id, kind=kind, status='queued',
            deadline_at=deadline_at, created_at=int(time.time()*1000), attempt=0))
        self.effects.rpush(self.bus.pending_key(slot), mid)
        self.audit('queued', slot=slot, message_id=mid, request_id=request_id, detail=body)
        return mid

    def transaction(self, identity, action, *, event_stream_id=None, authorization=None):
        intent = self.prefix + ':intent:' + _short(identity, 'intent_id')
        phase = self.bus.r.hget(intent, 'phase')
        if phase == 'applied':
            if event_stream_id:
                # A duplicate Stream entry may exist after ambiguous producer
                # registration. Its durable effect intent proves ACK is now safe.
                self.bus.r.xack(self.events, self.group, event_stream_id)
            return None
        if phase == 'applying':
            raise BusError('ambiguous applying intent; run must remain held: ' + identity)
        if not phase:
            real = self.f.r
            self.effects = Effects(real)
            self.f.r = self.effects
            self.context = {'intent_id': identity}
            self.queue_index = 0
            try:
                answer = action()
                if event_stream_id:
                    self.effects.command('XACK', self.events, self.group, event_stream_id)
                plan = dict(commands=self.effects.commands,
                            authorization=authorization or self.context.get('authorization'),
                            commit_gate=self.context.get('commit_gate'))
            finally:
                self.f.r = real
                self.effects = None
                self.context = None
            if not plan['commands']:
                return answer
            # No effects have occurred. Failure before this write leaves event PEL
            # replayable; randomness in a discarded plan never authorized work.
            self.bus.r.hset(intent, mapping={'phase': 'prepared', 'plan': dumps(plan)})
        else:
            answer = None
        self.apply(intent)
        return answer

    def apply(self, intent):
        phase = self.bus.r.hget(intent, 'phase')
        if phase == 'applied':
            return
        if phase != 'prepared':
            raise BusError('ambiguous/unrecognized intent: ' + intent)
        raw = self.bus.r.hget(intent, 'plan')
        self.bus.r.hset(intent, 'phase', 'applying')
        # Response loss: inspect phase on restart. An applied intent is complete;
        # anything still applying is held, never blindly rerun RPUSH/XADD.
        self.bus.r.eval(LUA_APPLY, 1, intent, raw)

    def convert_legacy(self):
        """LMOVE LEFT->RIGHT preserves legacy RPUSH FIFO and a reliable processing list."""
        processing = self.prefix + ':legacy-processing'
        raw = self.bus.r.lindex(processing, 0)
        if raw is None:
            raw = self.bus.r.lmove('fx:events', processing, 'LEFT', 'RIGHT')
        if raw is None:
            return False
        identity = 'legacy-' + digest(raw)
        try:
            event = json.loads(raw)
            if not isinstance(event, dict):
                raise ValueError('legacy event must be an object')
        except ValueError as exc:
            event = {'kind': 'legacy_invalid', 'error': str(exc), 'raw': raw}
        post_event(self.bus, event, identity)
        self.bus.r.lrem(processing, 1, raw)  # only after deterministic registration
        return True

    def step(self, block_ms=1000):
        self.convert_legacy()
        rows = self.reader.xreadgroup(self.group, self.consumer, {self.events: self.pending_cursor}, count=1)
        if not rows or not rows[0][1]:
            rows = self.reader.xreadgroup(self.group, self.consumer, {self.events: '>'}, count=1, block=block_ms)
        if rows and rows[0][1]:
            sid, fields = rows[0][1][0]
            identity = fields['event_id']
            phase = self.bus.r.hget(self.prefix + ':intent:' + identity, 'phase')
            if phase == 'applying':
                # Preserve this PEL record for operator inspection but allow control
                # events to be handled. A poison/ambiguous event never gets ACKed.
                self.f.pause('ambiguous effect intent held for inspection: ' + identity)
                self.held_events.add(identity)
                self.pending_cursor = sid
                return True
            ev = json.loads(fields['data'])
            def handle():
                try:
                    self.handle(ev)
                except (ValueError, TypeError, KeyError) as exc:
                    # Reject malformed ingress visibly. Planning has no external
                    # side effects; business validators already validate dispatch
                    # before queueing it. Infrastructure errors remain fail closed.
                    self.effects.values.clear()
                    self.effects.commands.clear()
                    self.rotation_pending.clear()
                    self.audit('event.rejected', error=str(exc), level='error', detail=ev)
                    self.reply(ev, {'status': 'rejected', 'reason': str(exc)})
            self.transaction(fields['event_id'], handle, event_stream_id=sid)
            self.pending_cursor = sid
            # Filesystem/session retirement is deliberately outside the Redis intent.
            # Crash at this boundary holds recovery; never repeats runtime execution.
            for slot in list(self.rotation_pending):
                self.rotation_pending.remove(slot)
                self.f.finish_rotation(slot)
            return True
        return False

    def handle(self, ev):
        from cmux_fleet import fleet as legacy, pi_adapter
        kind, slot = ev.get('kind'), ev.get('agent')
        delivery = ev.get('delivery') if isinstance(ev.get('delivery'), dict) else {}
        spec = ev.get('spec') if isinstance(ev.get('spec'), dict) else {}
        candidate = ev.get('candidate')
        error_record = kind == 'candidate' and isinstance(candidate, dict) and 'error' in candidate
        self.audit(kind or 'invalid_event', slot=slot or '',
                   request_id=delivery.get('request_id') or spec.get('id') or '',
                   correlation_id=ev.get('request_id', ''), message_id=delivery.get('message_id', ''),
                   delivery_id=delivery.get('delivery_id', ''), generation=ev.get('generation'),
                   level='error' if error_record else 'info', detail=ev)
        if kind in ('bridge_session', 'startup', 'availability', 'candidate', 'started', 'settled', 'bridge_fault'):
            info = self.f.agent(slot) if slot in self.f.names else {}
            if (not info or ev.get('run_id') != self.bus.run_id
                    or ev.get('generation') != int(info.get('generation', -1))):
                self.audit('event.stale', slot=slot or '')
                return
            if kind == 'bridge_session':
                # Fixed session binding: do not adopt a different same-generation Pi session.
                if info.get('thread') and info['thread'] != ev.get('thread'):
                    raise ValueError('same-generation session replacement is unsupported')
                pi_adapter.event(self.f.cfg, slot, ev['generation'], dict(ev, kind='pi_session'))
                self.effects.hset(legacy.key('agent', slot), {'thread': ev['thread'],
                    'transcript': ev['transcript'], 'bridge_available': 0})
                # Slot registration is completed by bridge only after this durable reply.
                self.reply(ev, {'status': 'registered'})
            elif ev.get('thread') != info.get('thread'):
                self.audit('event.stale_session', slot=slot)
            elif kind == 'startup':
                if info.get('state') != 'starting':
                    self.audit('startup.duplicate', slot=slot)
                    self.reply(ev, {'status': 'rejected', 'reason': 'startup belongs to an already settled or held runtime'})
                    return
                if (info.get('launch_ready', '1') != '1'
                        or ev.get('failed') or ev.get('aborted') or ev.get('text', '').strip() != 'READY'):
                    self.f.setf(slot, state='dead', runtime_ready=0, bridge_available=0)
                    self.f.finish('blocked', slot + ': startup failed before READY')
                    self.reply(ev, {'status': 'rejected', 'reason': 'startup did not settle with READY'})
                else:
                    self.f.pi_metadata(slot, ev)
                    self.f.setf(slot, state='idle', runtime_ready=1, idle_confirmed=1, bridge_available=0,
                                current_profile=info.get('runtime_profile', ''))
                    self.reply(ev, {'status': 'ready'})
            elif kind == 'availability':
                self.f.setf(slot, bridge_available=int(ev.get('idle') is True))
            elif kind == 'bridge_fault':
                self.hold(slot, ev.get('reason', 'bridge disconnected'))
            elif kind == 'started':
                try:
                    env = ev['delivery']
                    self.authorization(env)
                    self.f.r.hset(self.bus.msg_key(env['message_id']), mapping={
                        'started_at': int(time.time()*1000), 'execution_phase': 'running'})
                except (ValueError, KeyError, BusError) as exc:
                    self.audit('started.rejected', slot=slot, error=str(exc))
            elif kind == 'settled':
                self.settle(slot, ev)
                env = ev.get('delivery') or {}
                state = self.f.r.hget(self.bus.msg_key(env['message_id']), 'status') if env.get('message_id') else ''
                self.reply(ev, {'status': 'committed' if state == 'committed' else 'held' if state in ('uncertain', 'expired') else 'rejected'})
            # started/candidate are audit only, never ACK delivery or dispatch.
            return
        if kind in ('turn', 'pi_session'):
            self.audit('event.v1_rejected', slot=slot or '')
            return
        if kind == 'chrome':
            # An external broker call is a side effect and cannot be planned/replayed.
            self.reply(ev, {'status': 'rejected', 'reason': 'browser broker v2 transaction support is not implemented'})
            return
        if kind == 'stop':
            self.f.explicit_stop = True
            self.f.stopping = True
            self.f.finish('cancelled', 'explicit fleet stop; runtime retirement required')
            self.reply(ev, {'status': 'accepted', 'execution_stopped': False})
            return
        # Reuse original request/task/operator/pause/resume semantics and validation.
        if kind == 'operator_input':
            ev = dict(ev)
            if ev.get('run_id') != self.bus.run_id:
                self.reply(ev, {'status': 'rejected', 'reason': 'foreign run'})
                return
            # Allocate correlation-derived IDs: stable if the event is replayed.
            # Original Fleet generates request/task IDs only in this effect plan;
            # persistence makes them stable after the plan is saved.
        if kind not in ('operator_input', 'request', 'task', 'pause', 'resume', 'legacy_invalid'):
            self.reply(ev, {'status': 'rejected', 'reason': 'unsupported v2 event'})
            return
        if kind == 'legacy_invalid':
            return
        _request_id(ev.get('request_id'))
        before_values = copy.deepcopy(self.effects.values)
        before_commands = list(self.effects.commands)
        self.f.handle(ev, _v2_internal=True)
        raw = self.f.r.get('fx:reply:' + ev['request_id'])
        answer = json.loads(raw)
        if answer.get('status') == 'rejected':
            # A v2 byte-size rejection may occur when the original handler has
            # already planned request/task fields. Discard them with the queue.
            self.effects.values = before_values
            self.effects.commands = before_commands
            self.f.r.set('fx:reply:' + ev['request_id'], raw, ex=86400)
        self.reply(ev, answer)
        if answer.get('status') == 'accepted':
            self.audit('accepted', request_id=answer.get('id') or answer.get('task_id') or spec.get('id') or '',
                       correlation_id=ev['request_id'], slot=slot or '', generation=ev.get('generation'), detail=answer)

    def reply(self, ev, answer):
        correlation = ev.get('request_id')
        if correlation:
            try:
                _request_id(correlation)
            except ValueError:
                self.audit('reply.invalid_correlation', level='error')
                return
            self.effects.xadd(self.prefix + ':reply:' + correlation, {'data': dumps(answer)})

    def authorization(self, env):
        self.bus._authorization(env['slot'], env)
        msg = self.bus.message(env['message_id'])
        lease = self.bus.r.hgetall(self.bus.lease_key(env['slot']))
        names = ('run_id', 'slot', 'generation', 'session_id', 'message_id', 'delivery_id', 'stream_id', 'fence')
        if any(str(msg.get(k)) != str(env.get(k)) for k in names):
            raise BusError('settlement identity mismatch')
        if msg.get('consumer') != env.get('consumer'):
            raise BusError('settlement consumer mismatch')
        if lease.get('current_delivery') != env['delivery_id']:
            raise BusError('settlement stale lease')
        return msg, lease

    def release(self, env, result, outcome='completed'):
        self.effects.hset(self.bus.msg_key(env['message_id']), {'status': 'committed',
            'outcome': outcome, 'result': _json(result), 'committed_at': int(time.time()*1000)})
        self.effects.hset(self.bus.lease_key(env['slot']), {'status': 'idle', 'current_delivery': '',
            'message_id': '', 'stream_id': '', 'consumer': '', 'lease_until': 0})
        self.effects.command('XACK', self.bus.inbox_key(env['slot']), self.bus.slot_group(env['slot']), env['stream_id'])
        self.f.setf(env['slot'], state='idle', idle_confirmed=1, request='', bridge_available=0)
        self.audit('committed', slot=env['slot'], message_id=env['message_id'],
                   delivery_id=env['delivery_id'], request_id=env['request_id'], detail=result)

    def remember(self, slot, business_value):
        # Preserve the original recent-reply input to the handoff skeleton. Store
        # candidate business data, before Python injects service correlation IDs.
        self.effects.rpush('fx:replies:' + slot, dumps(business_value)[-4000:])
        self.effects.ltrim('fx:replies:' + slot, -5, -1)

    def settle(self, slot, ev):
        from cmux_fleet import fleet as legacy
        env = ev.get('delivery')
        if not isinstance(env, dict) or env.get('slot') != slot:
            self.audit('settle.invalid_identity', slot=slot)
            return
        try:
            msg, lease = self.authorization(env)
        except (BusError, ValueError, KeyError) as exc:
            self.audit('settle.rejected', slot=slot, error=str(exc))
            return
        now = int(time.time()*1000)
        if msg['status'] != 'received' or lease.get('status') != 'received':
            self.audit('settle.late', slot=slot, message_id=env['message_id'], detail=ev)
            return
        if now >= min(msg['deadline_at'], int(lease['lease_until'])):
            self.hold(slot, 'deadline/lease expired before settlement validation', public_status='timeout')
            return
        self.context['authorization'] = {
            self.bus.msg_key(env['message_id']): dict({k: str(env[k]) for k in
                ('run_id', 'slot', 'generation', 'session_id', 'message_id', 'delivery_id', 'stream_id', 'fence', 'consumer')}, status='received'),
            self.bus.lease_key(slot): {'current_delivery': env['delivery_id'],
                                     'generation': str(env['generation']), 'session_id': env['session_id'], 'status': 'received'}}
        self.context['commit_gate'] = {'message': self.bus.msg_key(env['message_id']),
            'lease': self.bus.lease_key(slot), 'inbox': self.bus.inbox_key(slot),
            'group': self.bus.slot_group(slot), 'stream_id': env['stream_id'], 'consumer': env['consumer']}
        candidates = ev.get('candidates')
        purpose = msg['body'].get('purpose')
        error = None
        if ev.get('aborted') is not False or ev.get('failed') is not False or ev.get('unfinished'):
            self.hold(slot, 'aborted/error/unfinished settlement; execution stop unconfirmed', public_status='failed')
            return
        if purpose == 'handoff':
            self.release(env, {'handoff': 'settled'})
            self.f.setf(slot, state='handoff')
            self.rotation_pending.add(slot)
            return
        if not isinstance(candidates, list) or len(candidates) != 1:
            error = 'exactly one structured candidate is required'
        else:
            candidate = candidates[0]
            tool = 'fleet_decision' if slot == 'supervisor' else 'fleet_result'
            if not isinstance(candidate, dict) or candidate.get('tool') != tool or not isinstance(candidate.get('value'), dict):
                error = 'candidate tool/role/shape mismatch'
        value = {} if error else copy.deepcopy(candidates[0]['value'])
        history_value = copy.deepcopy(value)
        text = dumps(value)
        self.context['bound_request'] = env['request_id']
        self.context['bound_slot'] = slot
        self.context['bound_message'] = env['message_id']
        self.context['bound_delivery'] = env['delivery_id']
        self.f.pi_metadata(slot, ev)
        info = self.f.agent(slot)
        if slot == 'supervisor':
            if self.f.service and not error:
                for field in ('dispatch', 'skip'):
                    if not isinstance(value.get(field, []), list):
                        error = field + ' must be a list'
                for item in (value.get('dispatch', []) + value.get('skip', [])) if not error else []:
                    if not isinstance(item, dict) or item.get('request_id', env['request_id']) != env['request_id']:
                        error = 'service decision may route only its bound request'; break
                    item['request_id'] = env['request_id']
                if not error and len(value.get('dispatch', [])) + len(value.get('skip', [])) > 1:
                    error = 'one route or skip per bound service request'
                if not error:
                    text = dumps(value)
            if not error:
                try:
                    if self.f.service:
                        legacy.parse_service_decision(text, self.f.labels() + list(self.f.r.hgetall(legacy.NAMES)))
                    else:
                        legacy.parse_decision(text, self.f.labels() + list(self.f.r.hgetall(legacy.NAMES)), self.f.others_active())
                except (ValueError, TypeError, KeyError) as exc:
                    error = str(exc)
            if error:
                self.audit('candidate.invalid', slot=slot, error=error, detail=candidates)
            self.release(env, value, 'failed' if error else 'completed')
            if self.f.service:
                self.f.on_service_supervisor('invalid candidate' if error else text)
            else:
                self.f.on_supervisor('invalid candidate' if error else text)
            invalid = error or int(self.f.r.hget(legacy.SERVICE if self.f.service else legacy.TASK, 'bad_replies') or 0)
            if invalid:
                self.f.r.hset(self.bus.msg_key(env['message_id']), mapping={'outcome': 'failed'})
            else:
                self.remember(slot, history_value)
            if not invalid and not self.f.service and self.f.task().get('status') == 'completed':
                self.ui('supervisor', result_card({'status': 'ok', 'worker': self.f.label(slot),
                    'message_id': env['message_id'], 'result': {'text': self.f.task().get('reason', '产物已通过验收。')}},
                    execution_stopped=True))
            elif not invalid and value.get('reason'):
                summary = routing_card(value['reason'], kind='dispatched', slot=self.f.label(slot))
                summary['metadata']['event_id'] = 'ui-decision-' + env['message_id']
                self.ui('supervisor', summary)
        elif self.f.service:
            req = self.f.r.hgetall(legacy.REQ + env['request_id'])
            if req.get('status') != 'dispatched':
                self.audit('result.late', slot=slot, request_id=env['request_id'])
                self.hold(slot, 'public result already terminal; inspect before reusing slot')
                return
            if not error:
                try:
                    legacy.parse_reply(text, json.loads(req['spec'])['reply'])
                except ValueError as exc:
                    error = str(exc)
            # Schema failure is a terminal invalid result; no automatic repeat of tools.
            self.release(env, value, 'failed' if error else 'completed')
            if not error:
                self.remember(slot, value)
            self.publish(env['request_id'], 'invalid' if error else 'ok',
                         worker=self.f.label(slot), **({'error': error, 'raw': value} if error else {'result': value}))
        else:
            if not error and (not all(isinstance(value.get(k), list) and value[k]
                    and all(isinstance(item, str) for item in value[k]) for k in ('done', 'evidence'))
                    or not all(isinstance(value.get(k), str) for k in ('not_done', 'blocked'))):
                error = 'task result requires done/evidence lists and not_done/blocked text'
            self.release(env, value, 'failed' if error else 'completed')
            if error:
                self.f.finish('blocked', slot + ': ' + error)
            else:
                self.ui(slot, result_card({'status': 'ok', 'worker': self.f.label(slot),
                    'message_id': env['message_id'], 'result': value}, execution_stopped=True))
                self.remember(slot, value)
                self.f.report(slot, dumps(value), info.get('prev_label'))
                if info.get('prev_label'):
                    self.f.setf(slot, prev_label='')

    def publish(self, rid, status, **fields):
        from cmux_fleet import fleet as legacy
        if self.effects is None:
            return self.transaction('pub-' + digest(rid)[:32], lambda: self.publish(rid, status, **fields))
        # Tombstones are retained past public RESULT_TTL, so expiry never authorizes a rerun.
        tombstone = self.prefix + ':public:' + digest(rid)
        if self.f.r.get(tombstone):
            return
        req = self.f.r.hgetall(legacy.REQ + rid)
        now = time.time()
        rec = dict(id=rid, status=status, at=now,
                   elapsed_s=round(now-float(req.get('created') or now), 1), **fields)
        raw = dumps(rec)
        self.f.r.set(tombstone, raw)
        self.f.r.set(legacy.RESULT + rid, raw, ex=legacy.RESULT_TTL)
        self.f.r.rpush(legacy.RESULTS, raw)
        self.f.r.ltrim(legacy.RESULTS, -1000, -1)
        self.f.r.delete(legacy.REQ + rid)
        self.f.r.lrem(legacy.REQQ, 0, rid)
        stopped = status in ('ok', 'invalid', 'skipped')
        spec = json.loads(req.get('spec') or '{}')
        payload = spec.get('payload')
        title = payload.get('message') if isinstance(payload, dict) else None
        title = title if isinstance(title, str) else spec.get('instruction')
        title = title.splitlines()[0][:120] if isinstance(title, str) and title else None
        card = result_card(rec, task_title=title, execution_stopped=True if stopped else None, now=now)
        if status == 'ok' and isinstance(fields.get('result'), dict) and isinstance(fields['result'].get('reply'), str):
            card['render'] = sanitize_text(fields.get('worker', 'worker') + '：') + card['render']
        self.ui('supervisor', card)
        for slot in self.f.workers:
            if self.f.label(slot) == fields.get('worker'):
                self.ui(slot, card)
        self.audit('result', request_id=rid, slot=self.context.get('bound_slot') or self.f.resolve(fields.get('worker', 'supervisor')),
                   message_id=self.context.get('bound_message', ''), delivery_id=self.context.get('bound_delivery', ''),
                   level='info' if status=='ok' else 'error', detail={'public_result': rec, 'request': req})

    def hold(self, slot, reason, public_status=None):
        from cmux_fleet import fleet as legacy
        r = self.effects or self.bus.r
        state = r.hgetall(self.bus.lease_key(slot))
        mid = state.get('message_id')
        self.f.setf(slot, state='held', idle_confirmed=0, bridge_available=0)
        if mid:
            r.hset(self.bus.lease_key(slot), mapping={'status': 'uncertain'})
            r.hset(self.bus.msg_key(mid), mapping={'status': 'uncertain', 'uncertain_reason': reason})
            rid = r.hget(self.bus.msg_key(mid), 'request_id')
            if public_status and r.hgetall(legacy.REQ + rid):
                self.publish(rid, public_status, worker=self.f.label(slot), error=reason)
        self.audit('execution.held', slot=slot, message_id=mid or '', level='error', error=reason)
        if public_status is None:
            card = routing_card(self.f.label(slot) + ' 执行状态未确认，已暂停派工；原执行尚未确认停止。详情见 control。',
                                kind='notice', slot=self.f.label(slot), severity='warn')
            card['metadata']['event_id'] = 'ui-held-' + digest(slot + ':' + (mid or 'startup'))[:32]
            self.ui(slot, card)
            if slot != 'supervisor':
                self.ui('supervisor', card)
        self.f.pause(slot + ': ' + reason)

    def recover(self):
        # Replay prepared effects only; applied plans never run validators again.
        for intent in self.bus.r.scan_iter(match=self.prefix + ':intent:*'):
            if self.bus.r.hget(intent, 'phase') == 'prepared':
                try:
                    self.apply(intent)
                except redis.ResponseError as exc:
                    self.f.pause('prepared settlement invalid at recovery; held: ' + str(exc))
            elif self.bus.r.hget(intent, 'phase') != 'applied':
                self.f.pause('ambiguous intent requires inspection: ' + intent)
        for slot in self.f.names:
            info = self.f.agent(slot)
            state = self.bus.r.hgetall(self.bus.lease_key(slot))
            if state.get('current_delivery'):
                env = self.bus.message(state['message_id'])
                self.bus.mark_uncertain(slot, env, 'daemon restart; automatic execution replay forbidden')
                self.f.setf(slot, state='held', bridge_available=0, idle_confirmed=0)
            elif info.get('state') in ('starting', 'handoff', 'busy'):
                self.f.setf(slot, state='held', bridge_available=0, idle_confirmed=0)
        self.f.pause('same-run daemon restart recovered held; inspect sessions before resuming')
        self.audit('daemon.recovered', level='warn', execution_allowed=False)

    def deliver(self, slot):
        from cmux_fleet.fleet import REQ
        state = self.bus.r.hgetall(self.bus.lease_key(slot))
        if state.get('status') != 'idle':
            return
        mids = self.bus.pending(slot, 1)
        if not mids:
            return
        msg = self.bus.message(mids[0])
        if self.f.service and msg['kind'] != 'followup' and not self.f.r.hgetall(REQ + msg['request_id']):
            self.transaction('drop-' + msg['message_id'], lambda: self.cancel_queued(slot, msg, 'already terminal'))
            return
        env = self.bus.offer(slot, mids[0])
        if env:
            # Offer + lease is durable before updating legacy display state. Crash
            # between these boundaries is detected and held on same-run recovery.
            self.f.setf(slot, state='busy', msg_id=env['message_id'], busy_since=time.time(),
                        request=env['request_id'], delivered=int(self.f.agent(slot).get('delivered') or 0)+1,
                        bridge_available=0)
            self.audit('deliver', slot=slot, request_id=env['request_id'], message_id=env['message_id'],
                       delivery_id=env['delivery_id'], generation=env['generation'], detail=env['body'])

    def cancel_queued(self, slot, msg, reason):
        r = self.effects or self.bus.r
        r.hset(self.bus.msg_key(msg['message_id']), mapping={'status': 'committed',
            'outcome': 'cancelled', 'result': '{}', 'committed_at': int(time.time()*1000), 'cancel_reason': reason})
        r.lrem(self.bus.pending_key(slot), 0, msg['message_id'])

    def finish(self, status, reason):
        from cmux_fleet.fleet import TASK
        if self.effects is None:
            return self.transaction('finish-' + uuid.uuid4().hex, lambda: self.finish(status, reason))
        if self.f.service:
            self.f.pause(status + ': ' + reason)
            return
        self.f.r.hset(TASK, mapping={'status': status, 'reason': reason})
        for slot in self.f.names:
            for mid in self.f.r.lrange(self.bus.pending_key(slot), 0, -1):
                msg = dict(message_id=mid)
                self.cancel_queued(slot, msg, reason)
            if self.f.r.hget(self.bus.lease_key(slot), 'current_delivery'):
                self.hold(slot, status + ': ' + reason)
        self.audit('task.' + status, request_id=self.f.task().get('id', ''), detail={'reason': reason})

    def offer_handoff(self, slot, prompt):
        # Handoff runs at the existing idle boundary ahead of held business FIFO.
        mid = 'm-handoff-' + self.f.agent(slot)['handoff_id']
        deadline = int(float(self.f.agent(slot)['handoff_deadline'])*1000)
        deadline = max(deadline, int(time.time()*1000)+100)  # zero-timeout test may still retire immediately
        body = {'text': prompt, 'title': '填写交接单', 'mode': self.f.cfg['mode'], 'purpose': 'handoff'}
        # Register with the reviewed bus, then move only this known handoff reference
        # to the FIFO head before authorization. No second consumer/scheduler.
        self.bus.enqueue(slot, body, request_id=self.f.task().get('id') or 'handoff',
                         kind='followup', deadline_at=deadline, message_id=mid)
        self.bus.r.eval("local n=redis.call('LREM', KEYS[1], 1, ARGV[1]); "
                        "if n==1 then redis.call('LPUSH', KEYS[1], ARGV[1]) end", 1,
                        self.bus.pending_key(slot), mid)
        env = self.bus.offer(slot, mid)
        if env:
            self.f.setf(slot, msg_id=mid, bridge_available=0)

    def confirm_retired(self, slot):
        """Call only after cmux confirmed the old runtime process-tree/tab retirement."""
        state = self.bus.r.hgetall(self.bus.lease_key(slot))
        if state.get('current_delivery'):
            env = self.bus.message(state['message_id'])
            if state.get('status') not in ('uncertain', 'expired'):
                self.bus.mark_uncertain(slot, env, 'runtime retired by daemon')
            self.bus.resolve_uncertain(slot, env, 'cancelled', execution_stopped=True)
        self.audit('runtime.retired', slot=slot, execution_stopped=True)

    def retire(self, slot, reason):
        info = self.f.agent(slot)
        pane = info.get('pane')
        if not pane or not self.f.mgr.kill_pane(pane) or self.f.mgr.get_pane_status(pane) is not None:
            self.transaction('retire-failed-' + uuid.uuid4().hex, lambda: self.hold(slot, reason + '; retirement unconfirmed'))
            return False
        self.confirm_retired(slot)
        self.f.setf(slot, state='dead', runtime_ready=0, bridge_available=0, launch_ready=0)
        return True

    def tick(self):
        from cmux_fleet.fleet import REQ, REQQ
        now = time.time()
        # Timeouts and cancellation effects use the same intent machinery as results.
        def expire():
            if self.f.service:
                for rid in self.f.r.lrange(REQQ, 0, -1):
                    req = self.f.r.hgetall(REQ + rid)
                    if not req:
                        continue
                    limit = float(json.loads(req['spec']).get('timeout_s') or self.f.cfg['request_timeout'])
                    if now-float(req['created']) >= limit:
                        for slot in self.f.names:
                            state = self.f.r.hgetall(self.bus.lease_key(slot))
                            if state.get('message_id') and self.f.r.hget(
                                    self.bus.msg_key(state['message_id']), 'request_id') == rid:
                                self.hold(slot, 'request timeout; execution stop unconfirmed')
                        self.publish(rid, 'timeout', worker=req.get('worker') or 'supervisor',
                                     error='no valid answer within ' + str(int(limit)) + ' s')
            task = self.f.task()
            if task.get('status') == 'running' and now >= float(task['deadline']):
                self.finish('budget_exhausted', 'time budget exhausted')
            for slot in self.f.names:
                info = self.f.agent(slot)
                state = self.f.r.hgetall(self.bus.lease_key(slot))
                if info.get('state') == 'starting' and now >= float(info.get('startup_deadline') or 0):
                    self.f.setf(slot, state='dead', bridge_available=0, runtime_ready=0)
                    self.finish('blocked', slot + ' startup timed out before READY')
                elif state.get('current_delivery') and state.get('status') not in ('uncertain', 'expired'):
                    mid = state['message_id']
                    deadline = int(self.f.r.hget(self.bus.msg_key(mid), 'deadline_at'))
                    if int(now*1000) >= min(deadline, int(state['lease_until'])):
                        self.hold(slot, 'lease/delivery deadline expired; execution stop unconfirmed', public_status='timeout')
                    elif float(self.f.cfg['stall_timeout']) > 0 and now-self.f.last_activity(info) >= float(self.f.cfg['stall_timeout']):
                        self.hold(slot, 'agent stalled; execution stop unconfirmed', public_status='stalled')
        self.transaction('tick-' + uuid.uuid4().hex, expire)
        for slot in self.f.names:
            info = self.f.agent(slot)
            if info.get('handoff_id') and info.get('state') in ('handoff', 'held') and now >= float(info.get('handoff_deadline') or 0):
                self.f.finish_rotation(slot, timed_out=True)
        if now >= self.f.next_liveness:
            self.f.next_liveness = now+30
            for slot in self.f.names:
                info = self.f.agent(slot)
                if info.get('pane') and self.f.mgr.get_pane_status(info['pane']) is None:
                    self.transaction('closed-' + uuid.uuid4().hex, lambda slot=slot: self.hold(slot, 'runtime tab missing; stop evidence unavailable'))

    def run(self):
        marker = self.prefix + ':boot'
        first = self.bus.r.set(marker, 'provisioning', nx=True)
        control = None
        try:
            try:
                from cmux_fleet.control_view import start_control
                control = start_control(self.f.cfg)
            except Exception as exc:
                self.audit('control.unavailable', level='error', error=str(exc))
            if first:
                self.f.start_all()
                self.bus.r.set(marker, 'running')
            else:
                self.recover()  # no start_all, no Redis shutdown
            while not self.f.stopping:
                self.bus.r.set('fx:daemon:beat', str(time.time()))
                self.step()
                self.f.pump()
                self.tick()
            if self.f.explicit_stop:
                all_stopped = all([self.retire(slot, 'explicit fleet stop') for slot in self.f.names])
                if all_stopped:
                    self.bus.r.set(marker, 'stopped')
                else:
                    self.f.explicit_stop = False  # preserve Redis/evidence for unconfirmed stop
                    self.audit('stop.unconfirmed', level='error')
        finally:
            if control:
                try:
                    control.stop(timeout=1.0)
                except Exception:
                    pass
            self.close()
