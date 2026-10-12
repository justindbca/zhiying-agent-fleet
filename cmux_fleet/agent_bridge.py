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
from cmux_fleet import fleet, pi_adapter
from cmux_fleet.message_bus import BusError, _short
from cmux_fleet.runtime_v2 import bus_for, dumps, post_event, validate_config, wait_reply


class AgentBridge:
    def __init__(self, cfg, slot, generation, output):
        validate_config(cfg)
        if cfg.get('transport_version') != 2 or slot not in ['supervisor'] + cfg['workers']:
            raise ValueError('bridge requires a configured v2 slot')
        if type(generation) is not int or generation < 0:
            raise ValueError('invalid bridge generation')
        self.cfg, self.slot, self.generation = cfg, slot, generation
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

    def emit(self, payload):
        with self.write_lock:
            self.output.write(dumps(payload) + '\n')
            self.output.flush()

    def metadata(self, data):
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
        if kind not in ('started', 'candidate', 'settled', 'reject_delivery'):
            raise ValueError('unsupported bridge command')
        with self.lock:
            env = self.active
        if env is None:
            raise ValueError('no active delivery; manual turns cannot publish fleet results')
        fields = {k: data[k] for k in ('candidates', 'failed', 'aborted', 'unfinished',
                                      'ctx_used', 'ctx_window', 'candidate', 'reason') if k in data}
        fields['delivery'] = env
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
                        self.emit({'kind': 'ui', 'stream_id': sid, 'event_id': fields['event_id'],
                                   'card': json.loads(fields['data'])})
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
