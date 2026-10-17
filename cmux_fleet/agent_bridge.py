#!/usr/bin/env python3
"""Persistent, bound Pi JSONL/Redis adapter. No scheduling and no message files.

Three dedicated persistent blocking readers (inbox, UI, command replies) plus a
persistent producer pool keep blocking I/O out of Pi's event loop. Receipt retains
inbox PEL. On transport failure execution is disabled and never replayed; recovery
requires daemon reconciliation. Only this child's connections/threads are closed.
"""
import argparse
import json
from pathlib import Path
import sys
import threading
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import redis
from cmux_fleet import fleet, opencode_adapter, pi_adapter
from cmux_fleet.message_bus import BusError, _short
from cmux_fleet.runtime_v2 import bus_for, dumps, post_event, validate_config, wait_reply


class AgentBridge:
    def __init__(self, cfg, slot, generation, output):
        validate_config(cfg)
        if cfg.get('transport_version') != 2 or slot not in ['supervisor'] + cfg['workers']:
            raise ValueError('bridge requires a configured v2 slot')
        if type(generation) is not int or generation < 0:
            raise ValueError('invalid bridge generation')
        # Only in-process bridges: never fall back to Pi identity for another CLI.
        cli = fleet.agent_cli(cfg, slot)
        if cli not in ('pi', 'opencode'):
            raise ValueError('no in-process bridge for CLI ' + repr(cli))
        self.cfg, self.slot, self.generation, self.cli = cfg, slot, generation, cli
        self.r = fleet.connect(cfg)
        self.bus = bus_for(cfg, self.r)
        self.consumer = 'bridge-' + slot + '-g' + str(generation)
        self.output = output
        self.write_lock = threading.Lock()
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.ready_event = threading.Event()
        self.idle_event = threading.Event()
        self.faulted = False
        self.active = None
        self.binding = None
        self.threads = []
        self.readers = [self.bus.blocking_client(1000) for _ in range(3)]
        self.ui_cursor = '0-0'
        self.permissions = {}  # opencode request id -> class reported by this bridge

    def emit(self, payload):
        with self.write_lock:
            self.output.write(dumps(payload) + '\n')
            self.output.flush()

    def metadata(self, data):
        if self.cli == 'opencode':
            wire = opencode_adapter.event(self.cfg, self.slot, self.generation,
                                          dict(data, kind=opencode_adapter.KIND))
        else:
            wire = pi_adapter.event(self.cfg, self.slot, self.generation, dict(data, kind='pi_session'))
        _short(wire['thread'], 'session_id')
        binding = (wire['thread'], wire['transcript'])
        if self.binding is not None and binding != self.binding:
            raise ValueError('bridge cannot adopt a different session/transcript')
        self.binding = binding
        return wire

    def event(self, kind, fields=None, *, correlation=None, wait=False):
        fields = fields or {}
        if not self.binding:
            raise ValueError('bridge session is not bound')
        data = dict(fields, kind=kind, agent=self.slot, generation=self.generation,
                    run_id=self.bus.run_id, thread=self.binding[0], transcript=self.binding[1])
        correlation = correlation or 'e-' + uuid.uuid4().hex
        if wait:
            data['request_id'] = correlation
        post_event(self.bus, data, correlation)
        return wait_reply(self.bus, correlation, reader=self.readers[2]) if wait else {'status': 'recorded'}

    def initialize(self, data):
        if self.binding:
            raise ValueError('bridge already initialized')
        metadata = self.metadata(data)
        reply = self.event('bridge_session', metadata, wait=True)
        if reply.get('status') != 'registered':
            raise BusError('bridge binding not confirmed: ' + dumps(reply))
        self.bus.register_slot(self.slot, self.generation, self.binding[0])
        for target in (self.receive_loop, self.ui_loop, self.renew_loop):
            thread = threading.Thread(target=target, daemon=True)
            self.threads.append(thread)
            thread.start()
        return reply

    def command(self, data):
        if not isinstance(data, dict):
            raise ValueError('JSONL command must be an object')
        kind = data.get('kind')
        if kind == 'init':
            return self.initialize(data)
        if kind == 'shutdown':
            self.stop()
            return {'status': 'stopped'}
        self.metadata(data)
        if self.faulted:
            raise BusError('communication lost; delivery disabled pending reconciliation')
        if kind == 'startup':
            reply = self.event('startup', data, wait=True)
            if reply.get('status') == 'ready':
                self.ready_event.set()
            return reply
        if kind == 'operator_input':
            if self.slot != 'supervisor':
                raise ValueError('operator_input requires supervisor')
            return self.event(kind, {'text': data.get('text')}, correlation=data.get('correlation'), wait=True)
        if kind == 'availability':
            if data.get('idle') is True and self.active is None:
                self.idle_event.set()
            else:
                self.idle_event.clear()
            return self.event(kind, {'idle': data.get('idle') is True})
        if kind == 'permission':
            return self.permission(data)
        if kind in ('permission_resolved', 'permission_refused'):
            return self.permission_state(kind, data)
        if kind not in ('started', 'candidate', 'settled', 'reject_delivery', 'hold'):
            raise ValueError('unsupported bridge command')
        with self.lock:
            env = self.active
        if env is None:
            raise ValueError('no active delivery; manual turns cannot publish fleet results')
        fields = {k: data[k] for k in ('candidates', 'failed', 'aborted', 'unfinished',
                                      'ctx_used', 'ctx_window', 'candidate', 'reason') if k in data}
        if self.cli == 'opencode':  # durable opencode binding (inbox id prompt() returned, delivered ids)
            fields.update({k: data[k] for k in ('oc_message_id', 'inbox') if k in data})
        fields['delivery'] = env
        if kind == 'hold':
            # opencode: the execution mixed this delivery with other input (or its id is unknown):
            # outcome unattributable. Held and reported; never success, never re-sent.
            if self.cli != 'opencode':
                raise ValueError('hold comes from the opencode plugin only')
            reason = str(data.get('reason') or 'opencode turn unattributable')[:1000]
            self.bus.mark_uncertain(self.slot, env, reason)
            self.faulted = True
            return self.event('bridge_fault', {'reason': 'opencode hold: ' + reason})
        if kind == 'reject_delivery':
            self.bus.mark_uncertain(self.slot, env, data.get('reason') or 'Pi is busy; no injection')
            self.faulted = True
            return self.event('bridge_fault', {'reason': 'delivery collided with manual input; slot held'})
        reply = self.event(kind, fields, wait=kind == 'settled')
        if kind == 'settled':
            # Keep active correlation/renewal until daemon commit or held state is
            # observed. Never XACK just because the terminal event was published.
            self.idle_event.clear()
            if reply.get('status') == 'committed':
                with self.lock:
                    self.active = None
                self.emit({'kind': 'completed', 'message_id': env['message_id']})
            else:
                self.fault('settlement unconfirmed/held; no execution replay')
        return reply

    def permission(self, data):
        """opencode stopped on a permission request. Report it; never answer it here. The class is
        recomputed on this side (the plugin's opinion can only make it stricter)."""
        if self.cli != 'opencode':
            raise ValueError('permission requests come from the opencode plugin only')
        req = data.get('request')
        if not isinstance(req, dict) or not isinstance(req.get('id'), str) or not req['id']:
            raise ValueError('permission requires the opencode request id')
        cls = opencode_adapter.permission_class(req)
        if data.get('class') == 'delete':
            cls = 'delete'
        with self.lock:
            env = self.active
            self.permissions[req['id']] = cls
        # opencode 2.0.29 Permission.Request = {id, sessionID, action, resources[], save?, metadata?, source?, message?}
        # (measured, worker-1 api-contract-supplement.md); the older names stay for safety.
        keep = ('id', 'sessionID', 'action', 'resources', 'message', 'source', 'metadata', 'permission', 'patterns',
                'title', 'command')
        fields = {'request': {k: req[k] for k in keep if k in req}, 'class': cls, 'session': self.binding[0]}
        if env:
            fields['delivery'] = env
        return self.event('permission_request', fields)

    def permission_state(self, kind, data):
        """opencode closed a reported request (permission.replied, or cleared without a reply when the
        execution was interrupted / the instance stopped), or the plugin refused to forward a ruling
        (e.g. a reject that would also decline an open user-held delete). Reported for the daemon's
        held/escalation bookkeeping; a closed request id is never forwarded again."""
        if self.cli != 'opencode':
            raise ValueError('permission state comes from the opencode plugin only')
        rid = data.get('request_id')
        if not isinstance(rid, str) or not rid:
            raise ValueError('permission state requires the opencode request id')
        with self.lock:
            cls = self.permissions.get(rid)
            if kind == 'permission_resolved':
                self.permissions.pop(rid, None)
        if cls is None:  # not reported by this bridge (or already closed): nothing to tell the daemon
            return {'status': 'ignored'}
        cls = 'other' if cls == 'answered' else cls
        fields = {'request_id': rid, 'class': cls, 'session': self.binding[0]}
        fields['reason'] = str(data.get('reason') or '')[:300]
        if kind == 'permission_resolved':
            reply = data.get('reply')
            fields['reply'] = reply if reply in ('once', 'always', 'reject') else None
            # Who decided. opencode only rejects on its own (shutdown, session-wide cascade) and an interrupt clears
            # with no reply: 'unattributed' / 'none' are never a user decision. Unknown values degrade to 'unattributed'.
            decided = data.get('decided_by')
            fields['decided_by'] = decided if decided in ('fleet', 'tui_user', 'unattributed', 'none') else 'unattributed'
        else:
            fields['held_for_user'] = data.get('held_for_user') is True
        return self.event(kind, fields)

    def permission_card(self, card):
        """A daemon ruling on a non-delete request reaches the plugin only when it names this
        bridge's run / slot / generation / session and a request this bridge reported as 'other'.
        Delete-class requests are the user's: no ruling is ever forwarded for them."""
        rid = card.get('request_id')
        with self.lock:
            cls = self.permissions.get(rid)
        if (card.get('run_id') != self.bus.run_id or card.get('slot') != self.slot
                or card.get('generation') != self.generation or card.get('session_id') != self.binding[0]
                or cls != 'other' or card.get('class') != 'other'
                or type(card.get('decision')) is not str or card.get('decision') not in ('once', 'reject')):
            return False
        with self.lock:
            self.permissions[rid] = 'answered'   # never forwarded again; closed by permission_resolved
        self.emit({'kind': 'permission_reply', 'request_id': rid, 'decision': card['decision']})
        return True

    def fault(self, reason):
        with self.lock:
            if self.faulted or self.stop_event.is_set():
                return
            self.faulted = True
            self.idle_event.clear()
            env = self.active
        self.emit({'kind': 'fault', 'reason': '通信中断，执行状态未确认；等待人工对账。'})
        try:
            if env:
                self.bus.mark_uncertain(self.slot, env, reason[:1000])
            self.event('bridge_fault', {'reason': reason[:1000]})
        except (redis.RedisError, BusError, OSError):
            # Lease expiry/restart reconciles when Redis was unreachable. No retry
            # executes the active body or authorizes another delivery.
            pass

    def receive_loop(self):
        try:
            while not self.stop_event.is_set() and not self.faulted:
                if not self.ready_event.wait(.1) or not self.idle_event.wait(.1):
                    continue
                env = self.bus.receive(self.slot, self.consumer, generation=self.generation,
                    session_id=self.binding[0], reader=self.readers[0])
                if not env:
                    continue
                with self.lock:
                    self.active = env
                    self.idle_event.clear()
                if not env['execution_allowed']:
                    self.fault('recovered PEL may have executed; replay forbidden')
                    return
                self.emit({'kind': 'delivery', 'delivery': env})
        except (redis.RedisError, BusError, ValueError, OSError) as exc:
            self.fault(str(exc))

    def renew_loop(self):
        period = min(10, self.bus.lease_ms/3000)
        while not self.stop_event.wait(period) and not self.faulted:
            try:
                with self.lock:
                    env = self.active
                if not env:
                    continue
                msg = self.bus.message(env['message_id'])
                if msg.get('status') == 'committed':
                    with self.lock:
                        self.active = None
                    self.emit({'kind': 'completed', 'message_id': env['message_id']})
                elif msg.get('status') in ('expired', 'uncertain'):
                    self.fault('daemon holds execution; stop confirmation required')
                elif not self.bus.renew(self.slot, env):
                    self.fault('lease expired; delivery held')
            except (redis.RedisError, BusError, ValueError, OSError) as exc:
                self.fault(str(exc))

    def ui_loop(self):
        try:
            while not self.stop_event.is_set() and not self.faulted:
                rows = self.readers[1].xread({self.bus.prefix + ':ui:' + self.slot: self.ui_cursor}, count=100, block=1000)
                for _, entries in rows:
                    for sid, fields in entries:
                        card = json.loads(fields['data'])
                        if isinstance(card, dict) and card.get('kind') == 'permission_reply':
                            if not self.permission_card(card):
                                self.emit({'kind': 'ui', 'stream_id': sid, 'event_id': fields['event_id'],
                                           'card': {'render': '权限裁定未转交（身份或类别不符）'}})
                            self.ui_cursor = sid
                            continue
                        self.emit({'kind': 'ui', 'stream_id': sid, 'event_id': fields['event_id'],
                                   'card': card})
                        self.ui_cursor = sid  # session branch entries provide replay dedup
        except (redis.RedisError, ValueError, OSError) as exc:
            self.fault(str(exc))

    def stop(self):
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        if self.active:
            try:
                self.bus.mark_uncertain(self.slot, self.active, 'Pi bridge shutdown; execution stop unconfirmed')
                self.event('bridge_fault', {'reason': 'Pi bridge shut down with active execution; stop unconfirmed'})
            except (redis.RedisError, BusError, OSError):
                pass  # lease expiry/recovery preserves the held association
        for thread in self.threads:
            thread.join(timeout=1.2)
        for client in self.readers + [self.r]:
            client.close(); client.connection_pool.disconnect()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('config'); parser.add_argument('slot'); parser.add_argument('generation', type=int)
    args = parser.parse_args(argv)
    bridge = AgentBridge(fleet.load(args.config), args.slot, args.generation, sys.stdout)
    try:
        for line in sys.stdin:
            call_id = None
            try:
                if len(line.encode()) > 1000000:
                    raise ValueError('JSONL frame exceeds limit')
                data = json.loads(line)
                call_id = data.get('call_id')
                answer = bridge.command(data)
            except (ValueError, BusError, redis.RedisError, OSError) as exc:
                answer = {'status': 'rejected', 'reason': str(exc)}
            bridge.emit({'kind': 'reply', 'call_id': call_id, 'answer': answer})
            if bridge.stop_event.is_set():
                break
    finally:
        bridge.stop()


if __name__ == '__main__':
    main()
