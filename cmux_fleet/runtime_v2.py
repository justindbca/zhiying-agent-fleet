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
import re
import time
import uuid
from pathlib import Path

import redis

from cmux_fleet import claude_hook, cli_pairs, contracts
from cmux_fleet.message_bus import BusError, MessageBus, _json, _short, _request_id
from cmux_fleet.presentation import mask_secrets, result_card, routing_card, sanitize_text
from cmux_fleet import codex_queue, lifecycle_close, subfleet, tui_cards
from cmux_fleet import watchdog


def v2_cli_problem(cfg):
    """None when this supervisor/worker CLI pair and mode may run transport v2.

    Decided by the pair table config/cli_pairs.yaml (see cli_pairs.pair_problem); a pair
    it refuses is never silently replaced by another CLI/model or the old file mailbox."""
    return cli_pairs.pair_problem(cfg)


def validate_config(cfg):
    version = cfg.get('transport_version', 1)
    if type(version) is not int or version not in (1, 2):
        raise ValueError('transport_version must be 1 or 2')
    if version == 2:
        problem = v2_cli_problem(cfg)
        if problem:
            raise ValueError(problem)
        if cfg.get('allow_development_v2') is not True:
            raise ValueError('transport v2 requires --allow-development-v2; real SDK/TUI acceptance is pending')
        for name in watchdog.DEFAULTS:
            watchdog.setting(cfg, name)
        if cfg.get('prompt_table'):
            try:
                watchdog.parse_table(Path(cfg['prompt_table']).read_text(encoding='utf-8'))
            except OSError as exc:
                raise ValueError('cannot read --prompt-table: ' + str(exc))
    return version


def tui_slot(cfg, slot):
    """Official interactive CLIs (Codex/Claude) have no in-process bridge: the daemon
    receives their delivery and hands the business text to the TUI (typed for Codex and a
    Claude supervisor; through the Stop-hook mailbox for Claude workers, see claude_hook);
    the CLI's own turn-end hook (Codex notify / Claude Stop) settles it."""
    return cfg.get('transport_version') == 2 and (
        cfg.get('supervisor_cli' if slot == 'supervisor' else 'worker_cli') or 'codex') != 'pi'


TUI_REPORT_RETRIES = 2  # corrections for an official-CLI task worker report before blocking

IDENTITY = ('run_id', 'slot', 'generation', 'session_id', 'message_id', 'delivery_id',
            'stream_id', 'fence', 'consumer', 'request_id')


def _norm(text):
    return ' '.join(str(text).split())


def ready_reply(text):
    """Startup settles when the reply's last non-empty line is READY (markdown emphasis and a
    trailing period tolerated): models often put a sentence first. 'NOT READY' is not READY."""
    lines = [x.strip().strip('*_`').rstrip('.').strip() for x in str(text or '').splitlines() if x.strip()]
    return bool(lines) and lines[-1] == 'READY'


STARTUP_NUDGE = ('Your startup reply was not READY. Do no task work in this turn: '
                 'reply exactly READY (one word, nothing else).')


def turn_answers(body_text, inputs):
    """The turn must have been started by the typed delivery text (not a manual turn)."""
    head = _norm(body_text)[:120]
    return bool(head) and any(head in _norm(item) for item in inputs if isinstance(item, str))


WORKER_SECTIONS = re.compile(r'^[\s>*#_-]*(done|evidence|not done|blocked)[\s*_]*:[\s*_]*(.*)$', re.I)


def tui_candidates(slot, purpose, text, mode='task'):
    """Map an official CLI's final text to the structured candidate the daemon validates.

    The agent-facing reply formats are the original AGENTS.md ones (decision JSON for
    the supervisor, Done/Evidence/Not done/Blocked for task workers, the request's
    business JSON object for service workers). Unparseable text yields no candidate,
    so the daemon's existing invalid/blocked paths apply.
    """
    if purpose == 'handoff':
        return []
    try:
        value = json.loads(text[text.index('{'):text.rindex('}') + 1])
    except ValueError:
        value = None
    if slot == 'supervisor':
        return [{'tool': 'fleet_decision', 'value': value}] if isinstance(value, dict) else []
    if mode == 'service':  # the reply schema of the request is checked by the daemon (parse_reply)
        return [{'tool': 'fleet_result', 'value': value}] if isinstance(value, dict) else []
    if isinstance(value, dict) and 'done' in value and 'evidence' in value:
        return [{'tool': 'fleet_result', 'value': value}]
    # Contract tasks: a trailing JSON object (interface_change) and the Interfaces / Peer notices /
    # Question lines become extra keys, only when present (other reports are unchanged).
    extra, rest = contracts.last_json_object(text)
    extra = {k: extra[k] for k in contracts.REPORT_KEYS if k in extra} if isinstance(extra, dict) else {}
    lines = (rest if extra else text).splitlines()
    sections, current = {}, None
    for line in lines:
        m = contracts.EXTRA_SECTIONS.match(line)
        if m:
            current = None
            extra[{'interfaces': 'interfaces', 'peer notices': 'peer_notices'}.get(m.group(1).lower(), 'question')] \
                = m.group(2).strip()
            continue
        m = WORKER_SECTIONS.match(line)
        if m:
            current = m.group(1).lower()
            sections[current] = [m.group(2).strip()]
        elif current:
            sections[current].append(line.strip())
    items = lambda k: [x.lstrip('-*• ').strip() for x in sections.get(k, []) if x.lstrip('-*• ').strip()]
    value = {'done': items('done'), 'evidence': items('evidence'),
             'not_done': '\n'.join(items('not done')), 'blocked': '\n'.join(items('blocked')), **extra}
    return [{'tool': 'fleet_result', 'value': value}] if value['done'] and value['evidence'] else []


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
        self.startup_nudges = set()  # slots to re-ask for READY once, typed after the transaction
        self.pending_cursor = '0-0'
        self.held_events = set()
        self.renewed = {}
        self.cards_dirty, self.cards_refreshed = False, 0.0
        self.watchdog = watchdog.Watchdog(self)
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
        # Only Pi has the fleet_decision tool; official CLIs keep the AGENTS.md wording.
        if not tui_slot(self.slot_cfg(slot), slot):
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
                    self.startup_nudges.clear()
                    self.audit('event.rejected', error=str(exc), level='error', detail=ev)
                    self.reply(ev, {'status': 'rejected', 'reason': str(exc)})
            self.transaction(fields['event_id'], handle, event_stream_id=sid)
            self.pending_cursor = sid
            # Filesystem/session retirement is deliberately outside the Redis intent.
            # Crash at this boundary holds recovery; never repeats runtime execution.
            for slot in list(self.rotation_pending):
                self.rotation_pending.remove(slot)
                self.f.finish_rotation(slot)
            for slot in list(self.startup_nudges):  # pane typing / queueing is not a Redis effect either
                self.startup_nudges.remove(slot)
                pane = self.f.agent(slot).get('pane')
                if codex_queue.is_codex(self.slot_cfg(slot), slot):
                    try:
                        codex_queue.deliver(self.f, slot, 'startup-nudge-g' + str(self.f.agent(slot).get('generation')),
                                            STARTUP_NUDGE)
                    except codex_queue.QueueError as exc:
                        # No typing fallback: the startup deadline then blocks the slot as for no READY.
                        self.audit('startup.nudge_failed', slot=slot, level='error', error=str(exc)[:300])
                elif self.hooked(slot):
                    generation = int(self.f.agent(slot).get('generation') or 0)
                    claude_hook.post(self.bus.r, self.hook_keys(slot, generation),
                                     {'kind': 'nudge', 'generation': generation, 'text': STARTUP_NUDGE})
                elif pane:
                    from cmux_fleet.fleet import is_claude
                    self.f.mgr.send_prompt_to_idle(pane, STARTUP_NUDGE, bracketed=is_claude(self.f.runtime_cfg(slot), slot))
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
        if kind in ('bridge_session', 'startup', 'availability', 'candidate', 'started', 'settled', 'bridge_fault',
                    'turn_ended'):
            info = self.f.agent(slot) if slot in self.f.names else {}
            if (not info or ev.get('run_id') != self.bus.run_id
                    or ev.get('generation') != int(info.get('generation', -1))):
                self.audit('event.stale', slot=slot or '')
                return
            if kind == 'bridge_session':
                # Fixed session binding: do not adopt a different same-generation Pi session.
                if info.get('thread') and info['thread'] != ev.get('thread'):
                    raise ValueError('same-generation session replacement is unsupported')
                if tui_slot(self.slot_cfg(slot), slot):
                    self.tui_session(slot, ev)
                else:
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
                text_ok = ready_reply(ev.get('text', ''))
                walled = None
                if (info.get('launch_ready', '1') == '1' and not text_ok
                        and slot in self.f.workers + ['supervisor']):
                    # E: a wall instead of READY pauses resumable (Pi also reports the model error text).
                    walled = self.f.startup_wall(slot, '\n'.join(str(ev.get(k) or '') for k in ('text', 'error')))
                nudged = info.get('startup_nudged') == info.get('startup_deadline', '')
                if walled:
                    self.f.notify_later(walled)
                    self.reply(ev, {'status': 'rejected', 'reason': 'usage wall at startup: slot walled, fleet paused'})
                elif (not text_ok and info.get('launch_ready', '1') == '1' and not ev.get('failed')
                        and not ev.get('aborted') and not nudged):
                    # One correction per READY ask (keyed by its deadline), then block as before.
                    self.f.setf(slot, startup_nudged=info.get('startup_deadline', ''))
                    self.startup_nudges.add(slot)
                    self.audit('startup.nudged', slot=slot)
                    self.reply(ev, {'status': 'rejected', 'reason': 'startup reply was not READY; asked once more'})
                elif (info.get('launch_ready', '1') != '1'
                        or ev.get('failed') or ev.get('aborted') or not text_ok):
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
            elif kind == 'turn_ended':
                self.release_held(slot, ev)
            elif kind == 'started':
                try:
                    env = ev['delivery']
                    self.authorization(env)
                    self.f.r.hset(self.bus.msg_key(env['message_id']), mapping={
                        'started_at': int(time.time()*1000), 'execution_phase': 'running'})
                except (ValueError, KeyError, BusError) as exc:
                    self.audit('started.rejected', slot=slot, error=str(exc))
            elif kind == 'candidate' and error_record and candidate.get('tool') == 'model':
                # Pi model error (stopReason error): remember a wall for this delivery; its settled
                # (failed) event then ends as a wall instead of an unconfirmed hold.
                from cmux_fleet import brake
                text = str(candidate.get('error') or '')[:brake.TURN_WALL_MAX]
                if delivery.get('delivery_id') and brake.classify_wall(text):
                    self.f.setf(slot, wall_error=dumps({'delivery_id': delivery['delivery_id'], 'text': text}))
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
        if kind == 'peer':  # §21 broker verb: Redis-only effects (queue + lists), plannable unlike chrome
            from cmux_fleet import peer
            self.reply(ev, peer.broker(self.f, ev))
            return
        if kind == 'unsolicited':  # §21: a worker turn no delivery started; never a settlement
            from cmux_fleet import peer
            info = self.f.agent(slot) if slot in self.f.workers else {}
            if (not info or ev.get('run_id') != self.bus.run_id or ev.get('thread') != info.get('thread')
                    or ev.get('generation') != int(info.get('generation', -1))):
                self.audit('event.stale', slot=slot or '')
                return
            peer.unsolicited(self.f, slot, str(ev.get('text') or ''))
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

    def tui_session(self, slot, ev):
        """Codex notify / Claude Stop binding: session id, and Claude's transcript only."""
        _short(ev.get('thread'), 'session_id')
        transcript = ev.get('transcript')
        if (self.slot_cfg(slot).get('supervisor_cli' if slot == 'supervisor' else 'worker_cli')) == 'claude':
            if not isinstance(transcript, str) or not transcript.endswith('.jsonl') or not Path(transcript).is_absolute():
                raise ValueError('Claude session binding requires its absolute transcript path')
        elif transcript != '':
            raise ValueError('Codex usage comes from its rollout files; transcript must be empty')

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

    def wall_settle(self, slot, ev, env, msg):
        """E: the turn ended on a usage wall (short official-CLI text, or a Pi model error for this delivery).
        The turn is over, so execution has stopped: commit the delivery as `walled` (result not applied, never
        a report), trip the brake, and queue the instruction again ONCE for after the pause / downgrade. A
        replayed settled event finds the message committed (settle.late), so nothing is queued twice."""
        from cmux_fleet import brake
        text = ev.get('wall_text') if isinstance(ev.get('wall_text'), str) else ''
        if not text:
            pending = json.loads(self.f.agent(slot).get('wall_error') or 'null') or {}
            text = pending.get('text', '') if pending.get('delivery_id') == env['delivery_id'] else ''
        if not text or len(text.strip()) > brake.TURN_WALL_MAX or not brake.classify_wall(text):
            return False
        record = self.f.on_wall(slot, text, 'turn')
        self.release(env, {'wall': record['wall']}, 'walled')
        self.f.setf(slot, wall_error='')
        body = msg['body'].get('text') or ''
        if body:  # same request / kind / deadline / reply spec as the walled delivery: only the text gains a note
            self.queue_index += 1
            mid = 'm-' + digest(self.context['intent_id'] + ':' + str(self.queue_index))[:32]
            again = dict(msg['body'], text=body if slot == 'supervisor' else brake.WALL_RESUME + body)
            self.effects.hset(self.bus.msg_key(mid), dict(run_id=self.bus.run_id, slot=slot, message_id=mid,
                body=_json(again), request_id=msg['request_id'], kind=msg.get('kind') or again.get('purpose') or '',
                status='queued', deadline_at=msg['deadline_at'], created_at=int(time.time()*1000), attempt=0,
                wall_of=env['message_id']))
            self.effects.rpush(self.bus.pending_key(slot), mid)
            self.audit('queued', slot=slot, message_id=mid, request_id=msg['request_id'], detail=again)
        self.audit('brake.wall_requeued', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'],
                   level='warn', detail={'scope': record['wall']['scope'], 'requeued': bool(body)})
        self.f.notify_later(record)
        return True

    def release_held(self, slot, ev):
        """Hold exit: the held delivery's own turn ended in its own session, so its execution has
        stopped. Release the slot (delivery cancelled, result NOT applied); the pause stays as is."""
        env = ev.get('delivery')
        lease = self.bus.r.hgetall(self.bus.lease_key(slot))
        problem = None
        if not isinstance(env, dict) or env.get('slot') != slot:
            problem = 'invalid delivery identity'
        elif self.f.agent(slot).get('state') != 'held' or lease.get('status') not in ('uncertain', 'expired'):
            problem = 'slot is not held'
        elif lease.get('current_delivery') != env.get('delivery_id') or lease.get('message_id') != env.get('message_id'):
            problem = 'not the held delivery'
        else:
            msg = self.bus.message(env['message_id'])
            if any(str(msg.get(k)) != str(env.get(k)) for k in IDENTITY if k != 'request_id'):
                problem = 'delivery identity mismatch'
        if problem is None:
            try:
                self.bus.resolve_uncertain(slot, env, 'cancelled', execution_stopped=True)  # idempotent on replay
            except (BusError, ValueError, KeyError, redis.ResponseError) as exc:
                problem = 'release failed: ' + str(exc)[:300]
        if problem:
            self.audit('hold.release_rejected', slot=slot, level='warn', error=problem)
            self.reply(ev, {'status': 'rejected', 'reason': problem})
            return
        self.f.setf(slot, state='idle', idle_confirmed=1, bridge_available=0, request='', watch_state='idle',
                    stall_escapes=0, watch_since=0)
        self.audit('hold.released', slot=slot, level='warn', message_id=env['message_id'],
                   delivery_id=env['delivery_id'], detail={'reason': 'held turn ended in its own session; '
                   'execution stopped, result not applied', 'paused': self.f.paused()})
        self.reply(ev, {'status': 'released', 'execution_stopped': True})

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
        if purpose != 'handoff' and self.wall_settle(slot, ev, env, msg):
            return
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
                        legacy.parse_service_decision(text, self.f.dispatch_targets())
                    else:
                        legacy.parse_decision(text, self.f.dispatch_targets(), self.f.others_active())
                except (ValueError, TypeError, KeyError) as exc:
                    error = str(exc)
            if error:
                self.audit('candidate.invalid', slot=slot, error=error, detail=candidates)
            self.release(env, value, 'failed' if error else 'completed')
            if self.f.service:
                self.f.on_service_supervisor(text, invalid=error)
            else:
                self.f.on_supervisor(text, invalid=error)
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
            retries = int(info.get('report_retries') or 0)
            if error and tui_slot(self.slot_cfg(slot), slot) and retries < TUI_REPORT_RETRIES:
                # Official CLIs report in free text (no fleet_result tool). Like the
                # supervisor's correction path, ask this worker to resend through the
                # normal FIFO delivery (new delivery/fence, same generation); never
                # invent done/evidence. Repeated failure still blocks the task.
                self.f.setf(slot, report_retries=retries + 1)
                self.audit('candidate.invalid', slot=slot, error=error, level='warn',
                           detail={'retry': retries + 1, 'limit': TUI_REPORT_RETRIES})
                self.f.queue(slot, 'Your last report is not in the AGENTS.md reply format (' + error
                             + '). Do not redo the work. Resend only the report, one heading per line: '
                             'Done: / Evidence: / Not done: / Blocked: (each heading followed by a colon).')
            elif error:
                self.f.finish('blocked', slot + ': ' + error)
            else:
                if retries:
                    self.f.setf(slot, report_retries=0)
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

    def replay_intents(self):
        # Replay prepared effects only; applied plans never run validators again.
        clean = True
        for intent in self.bus.r.scan_iter(match=self.prefix + ':intent:*'):
            if self.bus.r.hget(intent, 'phase') == 'prepared':
                try:
                    self.apply(intent)
                except redis.ResponseError as exc:
                    self.f.pause('prepared settlement invalid at recovery; held: ' + str(exc))
                    clean = False
            elif self.bus.r.hget(intent, 'phase') != 'applied':
                self.f.pause('ambiguous intent requires inspection: ' + intent)
                clean = False
        return clean

    def recover(self):
        self.replay_intents()
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

    # -- explicit start --resume (docs/design.md §17). A plain same-run restart still uses recover().
    def resume_run(self):
        """Reattach live official-CLI workers, respawn everything else as the next generation, and
        re-run the work that was in flight or queued once more (capped by max_attempts). The operator's
        explicit --resume is the authorization to replay; a plain daemon restart never replays."""
        from cmux_fleet.fleet import SERVICE
        if not self.replay_intents():
            self.audit('resume.refused', level='error', error='ambiguous effect intent; held like a plain restart')
            return self.recover()
        previous = self.bus.r.hget(SERVICE, 'reason') if self.f.paused() else ''
        unsettled = self.codex_unsettled()  # read before clear_slot abandons them and releases their leases
        lost = self.lost_dispatches()
        sup = self.f.agent('supervisor').get('pane')
        page = bool(sup) and self.f.mgr.get_pane_status(sup) is not None
        # A sub-fleet's adopted supervisor pane belongs to its parent fleet: reattach it like a worker.
        slots = self.f.workers + (['supervisor'] if subfleet.adopted(self.f.cfg, 'supervisor', self.f.agent(
            'supervisor').get('generation')) else [])
        reattach = [s for s in slots if page and self.reattachable(s)]
        for slot in self.f.names:
            if not self.clear_slot(slot, slot in reattach) and slot in reattach:
                reattach.remove(slot)  # its lease is still held: never leave it silently undeliverable
        for slot in reattach:
            self.f.setf(slot, state='idle', bridge_available=0, busy_since=time.time(), report_retries=0)
            self.audit('resume.reattached', slot=slot, generation=self.f.agent(slot).get('generation'))
        # A delegated slot's pane is run by its sub-fleet (resumed below): never replace it here.
        delegated = [s for s in self.f.workers if self.f.agent(s).get('state') == 'delegated']
        self.respawn([s for s in self.f.names if s not in reattach and s not in delegated], page)
        ran = self.codex_ran(unsettled)  # only now: respawn closed those sessions (never reattached)
        lost = [m for m in lost if m['message_id'] not in ran]
        self.f.resume()
        if self.f.service:
            self.transaction('resume-requests-' + uuid.uuid4().hex, lambda: self.requeue_requests(ran))
        elif self.f.task().get('status') == 'running':
            self.transaction('resume-task-' + uuid.uuid4().hex, lambda: self.brief(lost, ran))
        self.audit('daemon.resumed', level='warn', reattached=reattach, lost=len(lost), previous_pause=previous or '')
        subfleet.resume_children(self.f)  # children whose daemon is gone run again (--resume)

    def codex_unsettled(self):
        """Codex slots with a queued delivery never settled: [(slot, record, pane, lease message id or '')].
        Abandoning does not withdraw such an item (Codex has no official way to), so a live TUI would still
        run it next to the replay: clear_slot never reattaches these slots and respawn closes their session."""
        out = []
        for slot in self.f.names:
            info = self.f.agent(slot)
            if codex_queue.is_codex(self.slot_cfg(slot), slot) and codex_queue.unsettled(info):
                rec = codex_queue.record(info)
                lease = self.bus.r.hgetall(self.bus.lease_key(slot))
                current = lease.get('message_id') if lease.get('current_delivery') == rec['cq_delivery'] else ''
                out.append((slot, rec, info.get('pane') or '', current or ''))
        return out

    def codex_ran(self, unsettled):
        """After the old sessions are closed, the old rollout decides by the delivery's token: no turn -> it
        never ran (replayed); started -> interrupted by the close (replayed, like an aborted typed turn);
        completed -> it ran while the daemon was down: never replayed. A session still open (respawn could
        not close it; the task is blocked) is never replayed either. Returns {message_id: (slot, record, turn)}."""
        ran = {}
        for slot, rec, pane, current in unsettled:
            open_ = bool(pane) and self.f.mgr.get_pane_status(pane) is not None
            hit = codex_queue.token_turn(rec['cq_thread'], rec['cq_token'], self.f.sessions)
            if current and (open_ or (hit and hit['complete'])):
                ran[current] = (slot, rec, (hit or {}).get('turn', ''))
            event = 'resume.codex_open' if open_ else 'resume.codex_ran' if hit and hit['complete'] else (
                'resume.codex_interrupted' if hit else 'resume.codex_not_started')
            self.audit(event, slot=slot, level='warn', delivery_id=rec['cq_delivery'],
                       turn=(hit or {}).get('turn', ''), message_id=current,
                       replay='no' if current in ran else 'yes')
        return ran

    def lost_dispatches(self):
        """Task-mode worker instructions of the running task that were queued or in flight."""
        task = self.f.task()
        if self.f.service or task.get('status') != 'running':
            return []
        lost = []
        for slot in self.f.workers:
            state = self.bus.r.hgetall(self.bus.lease_key(slot))
            mids = ([state['message_id']] if state.get('current_delivery') else []) + self.bus.pending(slot)
            for mid in mids:
                msg = self.bus.message(mid)
                if msg.get('kind') == 'worker' and msg.get('request_id') == task['id']:
                    lost.append(msg)
        return lost

    def reattachable(self, slot):
        """Only official-CLI panes still running their CLI: a Pi bridge never survives a Redis outage."""
        from cmux_fleet.fleet import RESUME_CLIS
        info = self.f.agent(slot)
        pane = info.get('pane')
        if self.hooked(slot) and self.bus.r.hget(self.bus.lease_key(slot), 'current_delivery'):
            # The Escape that aborts its in-flight turn would also kill the waiting Stop hook and
            # leave the session with no delivery channel: start the next generation instead.
            return False
        return (tui_slot(self.slot_cfg(slot), slot) and info.get('runtime_ready') == '1' and not info.get('handoff_id')
                and info.get('state') in ('idle', 'busy', 'held') and bool(pane)
                and self.f.mgr.get_pane_status(pane) is not None and self.f.mgr.has_cli_process(pane, RESUME_CLIS))

    def clear_slot(self, slot, reattached):
        """Release the slot's lease and cancel its queue (re-queued afterwards). False: lease still held."""
        state = self.bus.r.hgetall(self.bus.lease_key(slot))
        released = True
        # A Codex delivery that was queued but never proven to run may still sit in this live session's
        # queue (Codex's queue is not ours to edit) and would run next to the replay: do not reattach
        # that session, respawn it (new thread; the old item can only run if that session is resumed).
        keep_out = (reattached and codex_queue.is_codex(self.slot_cfg(slot), slot)
                    and codex_queue.unsettled(self.f.agent(slot)))
        codex_queue.clear(self.f, slot, 'start --resume')  # its token can never settle anything now
        if keep_out:
            self.audit('resume.codex_unsettled', slot=slot, level='warn',
                       reason='queued delivery not proven to have run; session respawned, not reattached')
        if state.get('current_delivery'):
            pane = self.f.agent(slot).get('pane')
            if reattached and pane:
                self.f.mgr.send_key(pane, 'Escape')  # abort the old turn before its instruction is typed again
            try:
                env = self.bus.message(state['message_id'])
                if state.get('status') not in ('uncertain', 'expired'):
                    self.bus.mark_uncertain(slot, env, 'start --resume: replay authorized by the operator')
                self.bus.resolve_uncertain(slot, env, 'cancelled', execution_stopped=True)
            except (BusError, ValueError, KeyError) as exc:
                self.audit('resume.release_failed', slot=slot, level='error', error=str(exc))
                released = False
            if reattached:
                self.f.setf(slot, idle_confirmed=0)  # Escape is not proof the old turn stopped
        for mid in self.bus.pending(slot):
            self.cancel_queued(slot, {'message_id': mid}, 'start --resume: re-queued after reconcile')
        return released and not keep_out

    def respawn(self, slots, page):
        """Next generation of each slot in place; without a supervisor pane, rebuild the whole page."""
        from cmux_fleet import history
        from cmux_fleet.fleet import SERVICE, SUPERVISOR_LABEL, NAMES
        old = {}
        for slot in slots:
            info = self.f.agent(slot)
            pane = info.get('pane')
            if not info.get('generation'):  # never spawned (crash during provisioning)
                if slot == 'supervisor':
                    self.f.setf(slot, label=SUPERVISOR_LABEL)
                else:
                    self.f.init_worker(slot)
                continue
            generation, prev = int(info['generation']), self.f.label(slot)
            alive = bool(pane) and self.f.mgr.get_pane_status(pane) is not None
            snapshot = ''
            if alive:
                snapshot = self.f.run_dir / 'handoffs' / (slot + '-g' + str(generation) + '-resume.terminal.txt')
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                self.f.mgr.capture_to(pane, str(snapshot))
            history.record(self.f.run_dir, slot=slot, label=prev, generation=generation, handoff='',
                           snapshot=str(snapshot), source='resume', timed_out=False)
            label = (SUPERVISOR_LABEL if slot == 'supervisor' else slot) + '.' + str(generation + 1)
            self.f.setf(slot, generation=generation + 1, label=label, handoff_id='', retired_pane='',
                        prev_label='' if slot == 'supervisor' else prev)
            if info.get('rescue_launch') or info.get('rescue_relay'):
                # An advisor respawn cut off mid-handoff: the successor still starts with the sheet's
                # runtime (its domains are in advice_domains); the relay note / hint never leak into a later rotation.
                fields = dict(rescue_launch='', rescue_hint='', rescue_relay='')
                if info.get('rescue_launch'):
                    fields['runtime_launch'] = info['rescue_launch']
                self.f.setf(slot, **fields)
                self.audit('resume.rescue_launch', slot=slot, generation=generation + 1)
            if slot != 'supervisor':
                self.f.r.hset(NAMES, mapping={label: slot})
            old[slot] = pane if alive else None
        prompt = ('You are fleet agent {label}. Read AGENTS.md in the current directory first. This run was '
                  'resumed after an interruption; the daemon sends the current state next. In this turn do no '
                  'task work: your final reply must be exactly READY.')
        if not page:  # whole page lost (e.g. cmux restarted): close leftovers, then lay it out again
            for slot, pane in old.items():
                if pane and (not self.f.mgr.kill_pane(pane) or self.f.mgr.get_pane_status(pane) is not None):
                    self.f.setf(slot, state='dead', launch_ready=0)
                    return self.f.finish('blocked', 'start --resume could not close the old session of ' + slot)
                self.confirm_retired(slot)
            for slot in slots:
                self.f.spawn(slot, prompt.format(label=self.f.label(slot)))
            history.write_page(self.f.run_dir, self.f.history_slots())
            self.f.history_pane = self.f.mgr.arrange(self.f.agent('supervisor')['pane'],
                                                     [self.f.agent(w)['pane'] for w in self.f.workers],
                                                     history.page_url(self.f.run_dir))
            self.f.r.hset(SERVICE, mapping={'history_pane': self.f.history_pane or ''})
            return
        for slot in slots:
            new = self.f.spawn(slot, prompt.format(label=self.f.label(slot)), launch_held=True)
            pane = old.get(slot)
            if pane:  # the successor takes the old session's place, as in a handoff
                placed = self.f.mgr.tab_into(new, pane)
                if not placed or not self.f.mgr.kill_pane(pane) or self.f.mgr.get_pane_status(pane) is not None:
                    self.f.setf(slot, state='dead', launch_ready=0, retired_pane=pane,
                                retired_launch=self.f.agent(slot).get('runtime_launch') or '')
                    return self.f.finish('blocked', 'start --resume could not close the old session of ' + slot)
                self.confirm_retired(slot)
            elif slot != 'supervisor':
                peer = next((p for p in (self.f.agent(w).get('pane') for w in self.f.workers if w != slot)
                             if p and p != new and self.f.mgr.get_pane_status(p) is not None), None)
                if peer:
                    self.f.mgr.tab_into(new, peer, before=False)
                else:
                    self.f.mgr.place_right_of(new, self.f.agent('supervisor')['pane'])
            self.f.setf(slot, launch_ready=1)
        self.f.refresh_history()
        history_pane = self.f.r.hget(SERVICE, 'history_pane')
        if history_pane:
            self.f.mgr.dock_control(history_pane)  # the new control tab joins history again

    def progress(self, task):
        """Where a task stands on disk and in Redis: verified / missing artifacts, accepted dispatches.
        Shared by the resume brief and the night-switch brief (docs/design.md §17, §25)."""
        from cmux_fleet.fleet import check_artifacts, extended_checks
        head = lambda text: ' '.join(text.split())[:200]
        done = []
        for key in self.bus.r.scan_iter(match=self.prefix + ':message:*'):
            msg = self.bus.message(key.rsplit(':', 1)[1])
            if (msg.get('kind') == 'worker' and msg.get('request_id') == task['id']
                    and msg.get('status') == 'committed' and msg.get('outcome') == 'completed'):
                done.append((msg['committed_at'], '- ' + msg['slot'] + ': ' + head(msg['body']['text'])
                             + '\n  report: ' + dumps(msg.get('result') or {})[:600]))
        artifacts = json.loads(task.get('artifacts') or '[]')
        checked = [(a['path'], check_artifacts([a], self.f.workspace(), extended_checks(task)))
                   for a in artifacts]
        return ('\nDeclared artifacts verified on disk: ' + json.dumps([p for p, bad in checked if not bad])
                + '\nDeclared artifacts missing or failing: '
                + json.dumps([p + ' (' + '; '.join(bad) + ')' for p, bad in checked if bad], ensure_ascii=False)
                + '\nCompleted dispatches (their reports were accepted; do not redo them):\n'
                + ('\n'.join(x for _, x in sorted(done)) or '- none'))

    def brief(self, lost, ran=None):
        """Re-queue lost dispatches (attempt-capped) and brief the new supervisor generation. `ran`: worker
        instructions that ran to completion before the interruption (never re-queued, reported instead)."""
        task = self.f.task()
        cap = int(self.f.cfg['max_attempts'])
        head = lambda text: ' '.join(text.split())[:200]
        over = next((m for m in lost if int(m.get('replay') or 1) >= cap), None)
        if over:
            return self.f.finish('blocked', 'start --resume: the dispatch to ' + self.f.label(over['slot'])
                                 + ' was lost again after ' + str(int(over.get('replay') or 1)) + ' attempt(s) '
                                 '(max-attempts ' + str(cap) + '): ' + head(over['body']['text']))
        requeued = []
        for msg in lost:
            attempt = int(msg.get('replay') or 1) + 1
            mid = self.f.queue(msg['slot'], '[Supervisor instruction ' + task['id'] + '] ' + msg['body']['text'])
            self.effects.hset(self.bus.msg_key(mid), {'replay': attempt})
            requeued.append('- ' + self.f.label(msg['slot']) + ' (attempt ' + str(attempt) + ' of ' + str(cap)
                            + '): ' + head(msg['body']['text']))
        done = ['- ' + self.f.label(slot) + ': ' + head(self.bus.message(mid).get('body', {}).get('text', ''))
                for mid, (slot, _rec, _turn) in (ran or {}).items() if slot != 'supervisor']
        progress = self.progress(task)
        state = self.f.contract_state()
        self.f.queue('supervisor', '[Resumed task ' + task['id'] + '] This run was interrupted and resumed with '
                     'start --resume. You are a new supervisor generation: earlier decisions are not in your '
                     'context, so rely on this summary and the files on disk.'
                     + '\nGoal: ' + task.get('goal', '')
                     + '\nWorkers you can dispatch to: ' + ', '.join(self.f.labels())
                     + '\nShared output directory: ' + str(self.f.workspace())
                     + progress
                     + '\nLost dispatches the daemon already re-queued (do NOT dispatch them again):\n'
                     + ('\n'.join(requeued) or '- none')
                     + (('\nInstructions that already ran to completion before the interruption; their reports were '
                         'not received (check their files; do NOT dispatch them again):\n' + '\n'.join(done))
                        if done else '')
                     + (('\nInterface contracts are on: read CONTRACTS.md in your directory and '
                         + str(self.f.workspace() / 'contracts.json') + '. ' + contracts.table_line(state))
                        if state else '')
                     + '\nDispatch only work that is still missing. Reply with the JSON format from AGENTS.md.')

    def requeue_requests(self, ran=None):
        """Service: accepted requests without a public result run once more (unless replay is false, or a
        Codex item of theirs already ran to completion before the interruption)."""
        from cmux_fleet.fleet import REQ, REQQ
        cap = int(self.f.cfg['max_attempts'])
        done = {self.bus.message(mid).get('request_id') for mid in (ran or {})}
        for rid in self.f.r.lrange(REQQ, 0, -1):
            req = self.f.r.hgetall(REQ + rid)
            if not req:
                continue
            spec = json.loads(req['spec'])
            attempt = int(req.get('replay') or 1) + 1
            worker = req.get('worker') or 'supervisor'
            if rid in done:
                self.publish(rid, 'timeout', worker=worker, error='not replayed on resume: it already ran to '
                             'completion before the interruption and its result was not received')
            elif spec.get('replay') is False:
                self.publish(rid, 'timeout', worker=worker, error='not replayed on resume (request has replay: false)')
            elif attempt > cap:
                self.publish(rid, 'timeout', worker=worker, error='not replayed on resume: ' + str(cap)
                             + ' attempt(s) used (max-attempts ' + str(cap) + ')')
            else:
                # A fresh timeout window: the downtime does not count against the replay.
                self.f.r.hset(REQ + rid, mapping={'replay': attempt, 'status': 'routing', 'worker': '',
                                                  'created': time.time()})
                self.f.route(rid, spec)
                self.audit('request.replayed', request_id=rid, attempt=attempt)

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
            self.f.brake_count('calls')
            self.audit('deliver', slot=slot, request_id=env['request_id'], message_id=env['message_id'],
                       delivery_id=env['delivery_id'], generation=env['generation'], detail=env['body'])
            if tui_slot(self.slot_cfg(slot), slot):
                self.type_into(slot, env)

    def hooked(self, slot):
        """A Claude worker: deliveries go through its Stop-hook mailbox, never its input box."""
        return tui_slot(self.slot_cfg(slot), slot) and claude_hook.enabled(self.slot_cfg(slot), slot)

    def hook_keys(self, slot, generation):
        return claude_hook.keys(self.bus.prefix, slot, generation)

    def type_into(self, slot, env):
        """Receive an offered delivery for an official CLI and hand over only its business text.

        No message file and no transport IDs reach the model (a Codex delivery carries only its
        opaque delivery-token line). An official Codex agent gets the text through `codex queue`
        (codex_queue); a Claude worker from its Stop hook (mailbox, claude_hook); any other official
        CLI slot (a Claude supervisor) gets it typed into the TUI. The
        CLI's own turn-end hook settles against this delivery in Redis. Any failure holds the slot
        (fail closed).
        """
        consumer = 'tui-' + slot + '-g' + str(env['generation'])
        reason = 'TUI input not confirmed; prompt state unknown'
        try:
            got = self.bus.receive(slot, consumer, 1, generation=env['generation'],
                                   session_id=env['session_id'], reader=self.reader)
            if not got or got['delivery_id'] != env['delivery_id'] or not got['execution_allowed']:
                raise BusError('offered delivery was not received by this generation')
            self.renewed[slot] = time.monotonic()
            if codex_queue.is_codex(self.slot_cfg(slot), slot):
                # Codex: the TUI takes the message from its own queue; nothing is typed (codex_queue).
                got_q = codex_queue.deliver(self.f, slot, env['delivery_id'], got['body']['text'])
                self.audit('tui.queued', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'],
                           queued_id=got_q['queued_id'])
                return True
            pane = self.f.agent(slot).get('pane')
            if self.hooked(slot):
                k = self.hook_keys(slot, env['generation'])
                waiting = claude_hook.post(self.bus.r, k, {
                    'kind': 'delivery', 'generation': env['generation'], 'message_id': env['message_id'],
                    'delivery_id': env['delivery_id'], 'text': got['body']['text']})
                # armed=False is normal right after a settle (the hook is still reporting the turn). If
                # Escape / a hook timeout left the session idle, the next turn end of this session (any
                # typed line) picks it up; otherwise the watchdog's stall path applies.
                self.audit('tui.hooked', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'],
                           generation=env['generation'], armed=waiting)
                return True
            claude = self.slot_cfg(slot).get('supervisor_cli' if slot == 'supervisor' else 'worker_cli') == 'claude'
            if pane and self.f.mgr.send_prompt_to_idle(pane, got['body']['text'], bracketed=claude):
                self.audit('tui.typed', slot=slot, message_id=env['message_id'], delivery_id=env['delivery_id'])
                return True
        except (BusError, ValueError, KeyError, OSError, codex_queue.QueueError) as exc:
            reason += ': ' + str(exc)[:500]
        self.transaction('tui-input-' + env['delivery_id'], lambda: self.hold(slot, reason, public_status='failed'))
        return False

    def renew_tui(self):
        """The daemon is the receiving consumer for official CLIs, so it renews their
        received leases while the runtime tab exists (stall/liveness checks still hold)."""
        period = min(10.0, self.bus.lease_ms / 3000.0)
        for slot in self.f.names:
            if not tui_slot(self.slot_cfg(slot), slot) or time.monotonic() - self.renewed.get(slot, 0) < period:
                continue
            state = self.bus.r.hgetall(self.bus.lease_key(slot))
            if state.get('status') != 'received' or not state.get('consumer', '').startswith('tui-'):
                continue
            pane = self.f.agent(slot).get('pane')
            if not pane or self.f.mgr.get_pane_status(pane) is None:
                continue  # liveness tick holds a missing tab
            self.renewed[slot] = time.monotonic()
            try:
                renewed = self.bus.renew(slot, self.bus.message(state['message_id']))
            except BusError as exc:
                renewed, why = False, str(exc)
            else:
                why = 'lease expired'
            if not renewed:
                self.transaction('tui-renew-' + uuid.uuid4().hex,
                                 lambda slot=slot, why=why: self.hold(slot, why + '; execution stop unconfirmed'))

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
            codex_queue.clear(self.f, slot, 'task ' + status)  # its token can never settle (Codex's queue is not edited)
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
            if tui_slot(self.slot_cfg(slot), slot):
                self.type_into(slot, env)

    def slot_cfg(self, slot):
        """This slot's own CLI view: a roster/profile launch may run another worker CLI than the run default."""
        return self.f.runtime_cfg(slot)

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
        codex_queue.clear(self.f, slot, 'retire: ' + reason)
        info = self.f.agent(slot)
        pane = info.get('pane')
        if info.get('state') == 'reclaimed' and not pane:  # already retired by the idle reclaim
            self.confirm_retired(slot)
            return True
        if subfleet.adopted(self.f.cfg, slot, info.get('generation')) and pane:
            if not subfleet.hand_back(self.f):  # the pane goes back to the parent fleet, never killed
                return False
            self.confirm_retired(slot)
            self.f.setf(slot, state='dead', runtime_ready=0, bridge_available=0, launch_ready=0)
            return True
        if not pane or not self.f.mgr.kill_pane(pane) or self.f.mgr.get_pane_status(pane) is not None:
            self.transaction('retire-failed-' + uuid.uuid4().hex, lambda: self.hold(slot, reason + '; retirement unconfirmed'))
            return False
        self.confirm_retired(slot)
        self.f.setf(slot, state='dead', runtime_ready=0, bridge_available=0, launch_ready=0)
        return True

    def render_cards(self, refresh_every=5.0):
        """Official-CLI slots have no UI-only entry: render their cards off-model (tui_cards)."""
        slots = [slot for slot in self.f.names if tui_slot(self.f.cfg, slot)]
        if slots and tui_cards.render(self, slots, self.f.run_dir):
            self.cards_dirty = True
        if self.cards_dirty and time.monotonic() - self.cards_refreshed >= refresh_every:
            self.cards_dirty, self.cards_refreshed = False, time.monotonic()
            self.f.refresh_history()  # history page "Cards" view; reload only, no focus change

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
                if (info.get('state') == 'starting' and info.get('advice_hold') != '1'  # held for its first advisor sheet
                        and now >= float(info.get('startup_deadline') or 0)):
                    self.f.setf(slot, state='dead', bridge_available=0, runtime_ready=0)
                    self.finish('blocked', slot + ' startup timed out before READY')
                elif state.get('current_delivery') and state.get('status') not in ('uncertain', 'expired'):
                    mid = state['message_id']
                    deadline = int(self.f.r.hget(self.bus.msg_key(mid), 'deadline_at'))
                    if int(now*1000) >= min(deadline, int(state['lease_until'])):
                        self.hold(slot, 'lease/delivery deadline expired; execution stop unconfirmed', public_status='timeout')
                    # Stalls are the watchdog's (docs/design.md §24): Escape ladder, then kill + next generation.
        self.transaction('tick-' + uuid.uuid4().hex, expire)
        self.renew_tui()
        self.render_cards()
        self.watchdog.tick(now)  # screen / keys / kill / spawn: side effects, outside any Redis intent
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
            elif self.f.resume_requested:
                self.resume_run()  # explicit start --resume: reconcile, respawn, replay once
            else:
                self.recover()  # no start_all, no Redis shutdown
            while not self.f.stopping:
                self.bus.r.set('fx:daemon:beat', str(time.time()))
                self.step()
                self.f.pump()
                self.f.tick()  # Fleet.tick: roster reload, result.json, sub-fleets, then this tick
            if self.f.explicit_stop:
                children_stopped = subfleet.stop_children(self.f)  # descendants first, panes taken back
                all_stopped = all([self.retire(slot, 'explicit fleet stop') for slot in self.f.names]) and children_stopped
                if all_stopped:
                    self.bus.r.set(marker, 'stopped')
                    # An explicit stop finishes the run: start --resume refuses it, auto starts anew.
                    from cmux_fleet.fleet import save_result
                    self.f.write_result()
                    if not (self.f.run_dir / 'result.json').exists():
                        save_result(self.f.run_dir, {}, 'stopped',
                                    (self.f.lifecycle_close or {}).get('reason') or 'explicit fleet stop')
                else:
                    self.f.explicit_stop = False  # preserve Redis/evidence for unconfirmed stop
                    self.audit('stop.unconfirmed', level='error')
                lifecycle_close.finalize(self.f, all_stopped)  # closed_by/closed_at, notifications
        finally:
            if control:
                try:
                    control.stop(timeout=1.0)
                except Exception:
                    pass
            self.close()
