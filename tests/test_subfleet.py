"""Sub-fleets (docs/design.md §19): multi-fleet registries, pane handover, limits, tree stop/resume.

Two real private Redis (main fleet + sub-fleet, project-style runtime dirs) and two in-process
Fleet daemons. cmux is a fake *world* in which every fleet registry owns its own surfaces, so a
pane that one registry released is invisible to it (that is what makes the handover observable).
Hooks are the real ``claude_stop`` / ``notify`` entry points. This proves the wiring, not a real TUI.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import sys
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from test_fleet import write_rollout
from cmux_fleet import cmux_manager, fleet, redis_runtime, roster, runtime_v2, subfleet


# ------------------------------------------------------------------ fake cmux world
class World:
    def __init__(self, state_dir):
        self.n, self.alive, self.owner, self.ws_of = 0, set(), {}, {}
        self.sent, self.keys, self.mgrs, self.order, self.closed = [], [], {}, [], []
        self.state_dir = Path(state_dir)

    def manager(self, cfg):
        name = cfg.get('session') or fleet.SESSION
        if name not in self.mgrs:
            self.mgrs[name] = WorldMgr(self, name, cfg.get('agents_title') or 'Fleet agents')
        return self.mgrs[name]


class WorldMgr:
    def __init__(self, world, session, title):
        self.w, self.session, self.title = world, session, title
        self.ws = None
        self.meta_path = world.state_dir / (session + '.json')
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text('{}')
        self.arranged = None

    def _new(self, kind):
        self.w.n += 1
        sid = 'pane-' + str(self.w.n)
        self.ws = self.ws or ('ws-' + self.session)
        self.w.alive.add(sid)
        self.w.owner[sid] = self.session
        self.w.ws_of[sid] = self.ws
        return sid

    def owns(self, sid):
        return sid in self.w.alive and self.w.owner.get(sid) == self.session

    def spawn_interactive_cli(self, **kw):
        self.spawned = getattr(self, 'spawned', []) + [kw]
        return SimpleNamespace(success=True, pane_id=self._new('agent'), error=None)

    def spawn_window(self, window_name, command, metadata=None, start_directory=None):
        self.windows = getattr(self, 'windows', []) + [(window_name, command)]
        return SimpleNamespace(success=True, pane_id=self._new(window_name), error=None)

    def send_prompt_to_idle(self, pane, text, bracketed=False):
        self.w.sent.append((pane, text))
        return self.owns(pane)

    def send_key(self, pane, k):
        self.w.keys.append((pane, k))
        return self.owns(pane)

    def get_pane_status(self, pane):
        return pane if self.owns(pane) else None

    def has_cli_process(self, pane, names=('codex',)):
        return self.owns(pane)

    def place_right_of(self, a, b):
        return True

    def capture_to(self, pane, path):
        Path(path).write_text('screen of ' + pane)

    def arrange(self, sup, workers, url):
        self.arranged = (sup, list(workers), url)
        return self._new('history')

    def dock_control(self, pane):
        return True

    def reload_browser(self, pane):
        return True

    def tab_into(self, pane, peer, before=True):
        self.tabbed = getattr(self, 'tabbed', []) + [(pane, peer)]
        return self.owns(pane) and self.owns(peer)

    def kill_pane(self, pane):
        if self.w.owner.get(pane) != self.session:
            return False
        self.w.alive.discard(pane)
        self.w.owner.pop(pane, None)
        return True

    def rename(self, pane, label):
        self.renamed = getattr(self, 'renamed', []) + [(pane, label)]

    def get_or_create_session(self):
        return True

    # handover primitives (cmux_manager.CmuxSessionManager has the real ones)
    def workspace_id(self):
        return self.ws

    def release(self, sid):
        if self.w.owner.get(sid) != self.session:
            return None
        self.w.owner.pop(sid)
        return {'type': 'worker', 'label': 'fx-x'}

    def adopt(self, sid, peer, info):
        if sid not in self.w.alive or self.w.owner.get(sid) not in (None, self.session) or not self.owns(peer):
            return False
        self.w.owner[sid] = self.session
        self.w.ws_of[sid] = self.ws
        return True

    def _save_meta(self, meta):
        for sid in meta.get('surfaces', {}):
            if sid in self.w.alive and not self.w.owner.get(sid):
                self.w.owner[sid] = self.session

    def _tag(self, sid, **info):
        self.w.owner[sid] = self.session

    def place_after(self, ws):
        self.w.order.append((self.ws, ws))
        return True

    def session_exists(self):
        return any(self.owns(s) for s in list(self.w.alive))

    def close_all(self):
        for sid in [s for s, o in self.w.owner.items() if o == self.session]:
            self.kill_pane(sid)
        self.w.closed.append(self.session)
        return True


# ------------------------------------------------------------------ fixtures
def start_private(root, token=None):
    cfg = dict(redis_runtime.prepare(root, token or uuid.uuid4().hex[:8]), redis_server=shutil.which('redis-server'),
               transport_version=2)
    return cfg, fleet.start_redis(cfg)


def redis_down(cfg):
    return not subfleet.redis_up(cfg)


@pytest.fixture
def tree(tmp_path, monkeypatch):
    if shutil.which('redis-server') is None:
        pytest.skip('redis-server unavailable')
    root = Path('/tmp/cfs-' + uuid.uuid4().hex[:8])  # short: macOS Unix socket path limit
    monkeypatch.setattr(fleet, 'trust_claude_dir', lambda cwd: None)  # never touch ~/.claude.json
    world = World(tmp_path / 'cmux-state')
    monkeypatch.setattr(fleet, 'manager_for', world.manager)
    rcfg, r = start_private(root)
    run = tmp_path / 'fleet-20261013-000000-a1b2c3d4'
    (run / 'workspace').mkdir(parents=True)
    cfg = dict(rcfg, run=str(run), python=sys.executable, workers=['worker-1', 'worker-2'], mode='task',
               supervisor_cli='codex', worker_cli='claude', codex_home=str(tmp_path / 'codex-home'),
               allow_development_v2=True, stall_timeout=0, v2_lease_ms=300000, max_busy=0, max_attempts=2,
               name='研报', token='a1b2c3d4')
    path = run / 'config.json'
    fleet.save(path, cfg)
    t = SimpleNamespace(world=world, root=root, sessions=tmp_path / 'codex-home' / 'sessions',
                        claude=tmp_path / 'claude-projects', threads={}, fleets=[], tmp=tmp_path)
    f = fleet.Fleet(path, r, world.manager(cfg), sessions=t.sessions)
    t.f = f
    t.fleets.append(f)
    f.start_all()
    f.r.set(f.v2.prefix + ':boot', 'running')
    for slot in f.names:
        assert hook(t, f, slot, 'READY', prompt='startup')['status'] == 'ready'
    runtime_v2.post_event(f.v2.bus, dict(kind='task', request_id='new-task', spec={'goal': 'write the report'}), 'new-task')
    step(t)
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


def step(t, ms=10):
    for x in t.fleets:
        if not getattr(x, 'stopped', False):
            x.v2.step(ms)
            x.pump()


def drive(t, fn):
    """Run a (blocking) hook while every in-process daemon steps."""
    box = {}
    th = threading.Thread(target=lambda: box.setdefault('value', fn()))
    th.start()
    deadline = time.time() + 15
    while th.is_alive() and time.time() < deadline:
        step(t, 20)
    th.join(2)
    assert not th.is_alive()
    return box['value']


def hook(t, f, slot, text, *, prompt=None, thread=None, generation=None):
    """Fire the real turn hook the CLI in ``slot`` of fleet ``f`` was started with."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    thread = thread or t.threads.setdefault((str(f.run_dir), slot, gen), str(uuid.uuid4()))
    if prompt is None:
        prompt = [x for p, x in t.world.sent if p == f.agent(slot)['pane']][-1]
    if slot == 'supervisor' and fleet.agent_cli(f.cfg, slot) == 'codex':
        turn_id = 't-' + uuid.uuid4().hex
        write_rollout(t.sessions, thread, fleet.agent_cwd(f.cfg, slot), rows=[
            {'type': 'event_msg', 'payload': {'type': 'task_complete', 'turn_id': turn_id, 'last_agent_message': text}}])
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn_id,
                              'input-messages': [prompt], 'last-assistant-message': text})
        return drive(t, lambda: fleet.notify(f.path, slot, str(gen), payload, t.sessions))
    return claude_turn(t, f.path, slot, gen, thread, prompt, text)


def claude_turn(t, cfg_path, slot, gen, thread, prompt, text):
    transcript = t.claude / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [{'type': 'user', 'message': {'role': 'user', 'content': prompt}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    with transcript.open('a') as fh:
        fh.write('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(t, lambda: fleet.claude_stop(cfg_path, slot, str(gen), raw))


def no_launch(path, cfg, mgr):
    return SimpleNamespace(success=True, pane_id=None, error=None)


def spawn_m0(t, **kw):
    out = t.tmp / 'm0.md'
    rec = subfleet.spawn_child(t.f, 'worker-1', 'M0', 'Research five sources and merge them', 2,
                               output=str(out), launch=no_launch, **kw)
    return rec, out


def child_daemon(t, rec):
    """What `cmux-fleet daemon <child config>` does on its first boot, in process."""
    cfg = fleet.load(Path(rec['run']) / 'config.json')
    cf = fleet.Fleet(Path(rec['run']) / 'config.json', fleet.connect(cfg), t.world.manager(cfg), sessions=t.sessions)
    cf.lock = (Path(rec['run']) / 'daemon.lock').open('a+')  # a live daemon holds its run's lock
    fcntl.flock(cf.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    t.fleets.append(cf)
    cf.start_all()
    cf.r.set(cf.v2.prefix + ':boot', 'running')
    return cf


def parent_hook(t, text, prompt):
    """A turn end of the delegated pane: its CLI still runs the parent's worker-1 hook."""
    f = t.f
    gen = int(f.agent('worker-1')['generation'])
    thread = t.threads[(str(f.run_dir), 'worker-1', gen)]
    return claude_turn(t, f.path, 'worker-1', gen, thread, prompt, text)


def stop_child_like_daemon(cf):
    """The child daemon's explicit-stop branch of RuntimeV2.run, then daemon() shutting its Redis."""
    for _ in range(5):  # consume the stop event the parent posted (after anything queued before it)
        if cf.explicit_stop:
            break
        cf.v2.step(10)
    assert cf.explicit_stop
    assert subfleet.stop_children(cf)
    assert all(cf.v2.retire(s, 'explicit fleet stop') for s in cf.names)
    die(cf)
    subfleet.shutdown_redis(cf.cfg)  # daemon() shuts its Redis after an explicit stop


def die(cf):
    """The child daemon process is gone (its Redis may still be up)."""
    cf.v2.close()
    cf.stopped = True
    cf.lock.close()


# ------------------------------------------------------------------ acceptance 2 + 3: hand over and back
def test_worker_pane_becomes_subfleet_supervisor_and_comes_back(tree):
    t, f = tree, tree.f
    sid = f.agent('worker-1')['pane']
    worker_rules = (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text()
    rec, out = spawn_m0(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    # names, titles, tree layout on disk; latest.json is never written for a sub-fleet
    assert Path(rec['run']) == f.run_dir / 'sub' / 'M0'
    assert child_cfg['session'] == 'cmux-fleet:M0' and child_cfg['task_id'] == 'M0'
    assert child_cfg['agents_title'] == '研报 › worker-1 › 子 fleet M0'
    assert child_cfg['redis_socket'] != f.cfg['redis_socket'] and child_cfg['supervisor_cli'] == 'claude'
    # the parent stopped delivering to the slot and left a placeholder in its place
    info = f.agent('worker-1')
    assert info['state'] == 'delegated' and info['delegated_pane'] == sid and info['pane'] == rec['placeholder']
    assert (rec['placeholder'], sid) in f.mgr.tabbed
    assert any('已委派：子 fleet M0（看侧栏「研报 › worker-1 › 子 fleet M0」）' in label for _, label in f.mgr.renamed)
    assert f.mgr.get_pane_status(sid) is None and t.world.owner[sid] == 'cmux-fleet:M0'
    # the child daemon adopts the live pane top-left (no new supervisor process) and asks for READY
    cf = child_daemon(t, rec)
    assert cf.agent('supervisor')['pane'] == sid
    assert cf.mgr.arranged[0] == sid and len(cf.mgr.arranged[1]) == 2
    assert [kw['window_name'] for kw in cf.mgr.spawned] == ['fx-M0/worker-1', 'fx-M0/worker-2']
    assert ('cmux-fleet:M0' and (sid, 'fx-M0/supervisor-1') in cf.mgr.renamed)
    assert ('ws-cmux-fleet:M0', f.mgr.workspace_id()) in t.world.order  # sidebar: right after the parent
    assert 'Supervisor: observe, judge, dispatch' in (Path(child_cfg['supervisor_cwd']) / 'CLAUDE.md').read_text()
    role_change = [x for p, x in t.world.sent if p == sid][-1]
    assert 'sub-fleet M0' in role_change and 'READY' in role_change
    for w in cf.workers:
        assert hook(t, cf, w, 'READY', prompt='startup')['status'] == 'ready'
    # READY of the adopted pane goes through the PARENT worker-1 hook and lands in the child
    assert parent_hook(t, 'READY', role_change)['status'] == 'ready'
    assert cf.agent('supervisor')['runtime_ready'] == '1'  # READY, then the task is delivered at once
    assert cf.agent('supervisor')['thread'] == t.threads[(str(f.run_dir), 'worker-1', 0)]  # same session
    assert f.agent('worker-1')['state'] == 'delegated'
    step(t)
    task_msg = [x for p, x in t.world.sent if p == sid][-1]
    assert 'Research five sources and merge them' in task_msg and str(out) in task_msg
    decision = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': w, 'instruction': 'Write ' + w + '/part.md with two sources each'} for w in cf.workers]})
    assert parent_hook(t, decision, task_msg)['status'] == 'committed'
    step(t)
    for w in cf.workers:
        assert any('part.md' in x for p, x in t.world.sent if p == cf.agent(w)['pane'])
    assert not any('part.md' in x for p, x in t.world.sent if p == f.agent('worker-2')['pane'])
    # the sub-fleet finishes: deliverable written, task completed, result.json written
    out.write_text('merged findings')
    cf.v2.finish('completed', 'all parts merged')
    cf.write_result()
    subfleet.tick(f)  # parent sees result.json and asks the child to stop
    assert subfleet.record(rec['run'])['state'] == 'stopping'
    stop_child_like_daemon(cf)
    assert fleet.load(Path(rec['run']) / 'handback.json')['pane'] == sid
    assert sid in t.world.alive  # the adopted pane was released, never killed
    subfleet.tick(f)  # parent collects: pane back in place of the placeholder, child tabs closed
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'returned' and rec['outcome'] == 'completed'
    assert t.world.owner[sid] == 'cmux-fleet' and rec['placeholder'] not in t.world.alive
    assert 'cmux-fleet:M0' in t.world.closed and redis_down(child_cfg)
    assert (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text() == worker_rules
    info = f.agent('worker-1')
    assert info['pane'] == sid and info['state'] == 'starting' and not info['delegate_cfg']
    back = [x for p, x in t.world.sent if p == sid][-1]
    assert 'worker of the main fleet again' in back
    assert parent_hook(t, 'READY', back)['status'] == 'ready'  # same generation, same session
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['generation'] == '0'
    assert any('[Sub-fleet M0 completed]' in f.v2.bus.message(m)['body']['text']
               for m in f.v2.bus.pending('supervisor') + [f.v2.bus.slot_state('supervisor').get('message_id') or 'x']
               if f.v2.bus.message(m))
    assert 'cmux-fleet:M0' not in [p.stem for p in t.world.state_dir.glob('*.json')]


def test_delegated_slot_is_not_delivered_held_or_timed_out(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    f.queue('worker-1', '[Supervisor instruction ' + f.task()['id'] + '] Write worker-1/x.md now')
    f.next_liveness = 0
    for _ in range(3):
        f.pump()
        f.tick()
    info = f.agent('worker-1')
    assert info['state'] == 'delegated'  # liveness checks the placeholder, which is alive
    assert f.v2.bus.slot_state('worker-1')['status'] == 'idle' and f.inbox_depth('worker-1') == 1


def test_failed_subfleet_marks_failure_and_missing_deliverable_fails(tree):
    t, f = tree, tree.f
    rec, out = spawn_m0(t)
    cf = child_daemon(t, rec)
    cf.v2.finish('completed', 'claimed done')  # but the deliverable was never written
    cf.write_result()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'failed' and 'missing or empty' in rec['reason'] and rec['attempt'] == 1
    back = [x for p, x in t.world.sent if p == rec['pane']][-1]
    assert parent_hook(t, 'READY', back)['status'] == 'ready'
    rec2, _ = spawn_m0(t)  # a retry keeps the old run (history) and counts the attempt
    assert rec2['attempt'] == 2 and rec2['run'].endswith('M0-attempt-2')


def test_root_stop_stops_children_first_without_orphans(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    cf = child_daemon(t, rec)
    sid = rec['pane']
    # the child daemon died (no lock held) but its Redis is up: a tree stop still cleans it up
    die(cf)
    assert subfleet.stop_children(f, wait=1)
    assert redis_down(child_cfg)
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'failed' and rec['outcome'] == 'stopped'
    assert t.world.owner[sid] == 'cmux-fleet'  # taken back so the parent's own stop retires it
    assert all(f.v2.retire(s, 'explicit fleet stop') for s in f.names)
    assert sid not in t.world.alive


def test_root_resume_relaunches_running_children(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    subfleet.shutdown_redis(child_cfg)
    assert redis_down(child_cfg)
    launched = []
    subfleet.resume_children(f, launch=lambda path, cfg, mgr: launched.append(path))
    assert launched == [Path(rec['run']) / 'config.json'] and not redis_down(child_cfg)
    subfleet.shutdown_redis(child_cfg)


def test_child_resume_reattaches_adopted_supervisor_instead_of_respawning(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    for w in cf.workers:
        hook(t, cf, w, 'READY', prompt='startup')
    role_change = [x for p, x in t.world.sent if p == rec['pane']][-1]
    assert parent_hook(t, 'READY', role_change)['status'] == 'ready'
    die(cf)
    cfg = fleet.load(Path(rec['run']) / 'config.json')
    again = fleet.Fleet(Path(rec['run']) / 'config.json', fleet.connect(cfg), t.world.manager(cfg), sessions=t.sessions)
    t.fleets.append(again)
    again.resume_requested = True
    again.v2.resume_run()
    assert again.agent('supervisor')['pane'] == rec['pane'] and again.agent('supervisor')['generation'] == '0'
    assert rec['pane'] in t.world.alive


# ------------------------------------------------------------------ acceptance 4: limits with reasons
def test_limits_refuse_with_reasons(tree, tmp_path):
    t, f = tree, tree.f
    assert 'must be M<n>' in subfleet.check(f, 'worker-1', 'X1', 1)
    assert 'child_workers_max 3' in subfleet.check(f, 'worker-1', 'M0', 4)
    yaml = tmp_path / 'roster.yaml'
    yaml.write_text('workers:\n  - name: worker-1\n  - name: worker-2\nlimits:\n  max_agents_total: 4\n')
    f.cfg['roster'] = str(yaml)  # read at check time: hot
    assert 'max_agents_total 4: the tree has 3 agents' in subfleet.check(f, 'worker-1', 'M0', 2)
    yaml.write_text('workers:\n  - name: worker-1\n  - name: worker-2\nlimits:\n  max_children: 1\n')
    spawn_m0(t)
    assert 'max_children 1' in subfleet.check(f, 'worker-2', 'M1', 1)
    with pytest.raises(subfleet.Refused, match='max_children 1'):
        subfleet.spawn_child(f, 'worker-2', 'M1', 'other', 1, launch=no_launch)


def test_depth_limit_and_child_id_scope(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    assert 'max_depth 2: M0.1 would be level 3' in subfleet.check(cf, 'worker-1', 'M0.1', 1)
    assert "must be M0.<n>" in subfleet.check(cf, 'worker-1', 'M1', 1)
    f.cfg['limits'] = {'max_depth': 3}
    fleet.save(f.path, dict(fleet.load(f.path), limits={'max_depth': 3}))
    reason = subfleet.check(cf, 'worker-1', 'M0.1', 1)
    assert reason and 'max_depth' not in reason  # depth allowed now; the slot is just not READY


def test_pi_and_busy_slots_are_refused(tree):
    t, f = tree, tree.f
    f.cfg['worker_cli'] = 'pi'
    assert 'Pi bridge is bound' in subfleet.check(f, 'worker-1', 'M0', 1)
    f.cfg['worker_cli'] = 'claude'
    f.setf('worker-1', state='busy')
    assert 'must be idle and READY' in subfleet.check(f, 'worker-1', 'M0', 1)


def test_request_file_is_executed_by_the_daemon_tick(tree):
    t, f = tree, tree.f
    monkey = subfleet.default_launch
    subfleet.default_launch = no_launch
    try:
        rid = subfleet.request(f.run_dir, 'worker-1', 'M7', 'goal', 9)
        subfleet.tick(f)
        assert fleet.load(subfleet.answer_path(f.run_dir, rid)) == {
            'status': 'rejected', 'reason': 'child_workers_max 3: asked for 9 workers'}
        rid = subfleet.request(f.run_dir, 'worker-1', 'M7', 'goal', 1)
        subfleet.tick(f)
        assert fleet.load(subfleet.answer_path(f.run_dir, rid))['status'] == 'accepted'
    finally:
        subfleet.default_launch = monkey


def test_status_reports_the_tree(tree, capsys):
    t, f = tree, tree.f
    spawn_m0(t)
    fleet.status(f.cfg)
    out = json.loads(capsys.readouterr().out)
    assert out['subfleets']['M0']['state'] == 'running' and out['subfleets']['M0']['slot'] == 'worker-1'


def test_stale_generation_hook_is_not_routed(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    cfg, name, gen = subfleet.delegated_route(f.cfg, 'worker-1', 5)
    assert (name, gen) == ('worker-1', 5)
    cfg, name, gen = subfleet.delegated_route(f.cfg, 'worker-1', 0)
    assert (name, gen, cfg['task_id']) == ('supervisor', 0, 'M0')


# ------------------------------------------------------------------ pure pieces
def test_titles_keep_root_and_last_two_segments():
    assert subfleet.title(['研报', 'worker-1', 'M0']) == '研报 › worker-1 › 子 fleet M0'
    assert (subfleet.title(['研报', 'worker-1', 'M0', 'worker-2', 'M0.1'])
            == '研报 › worker-1 › M0 › worker-2 › 子 fleet M0.1')
    long = subfleet.title(['a very long main fleet name for research', 'worker-1', 'M0', 'worker-2', 'M0.1'], limit=50)
    assert long.startswith('a very long') and long.endswith(' › … › worker-2 › 子 fleet M0.1')


def test_ids_levels_and_sessions():
    assert subfleet.valid_child_id(None, 'M3') and not subfleet.valid_child_id(None, 'M0.1')
    assert subfleet.valid_child_id('M0', 'M0.2') and not subfleet.valid_child_id('M0', 'M1.2')
    assert subfleet.task_level('M0') == 2 and subfleet.task_level('M0.1') == 3
    assert subfleet.session_name() == 'cmux-fleet' and subfleet.session_name('M0.1') == 'cmux-fleet:M0.1'


def test_v2_matrix_allows_claude_supervisor_only_for_adopted_subfleets():
    base = dict(transport_version=2, allow_development_v2=True, supervisor_cli='claude', worker_cli='claude')
    assert runtime_v2.v2_cli_problem(base)
    assert runtime_v2.v2_cli_problem(dict(base, adopt={'pane': 'x'})) is None
    assert runtime_v2.v2_cli_problem(dict(base, adopt={'pane': 'x'}, mode='service'))


def test_roster_limits_are_validated(tmp_path):
    good = tmp_path / 'r.yaml'
    good.write_text('workers:\n  - name: worker-1\nlimits:\n  max_depth: 3\n')
    assert roster.load(good) == (['worker-1'], {}) and roster.load_limits(good) == {'max_depth': 3}
    bad = tmp_path / 'b.yaml'
    bad.write_text('workers:\n  - name: worker-1\nlimits:\n  max_depth: 0\n  whatever: 2\n')
    with pytest.raises(ValueError, match='max_depth, whatever'):
        roster.load(bad)


def test_claude_worker_session_name_is_stable_per_slot(tmp_path):
    cfg = {'run': str(tmp_path / 'fleet-20261013-000000-a1b2c3d4'), 'python': sys.executable, 'worker_model': 'sonnet'}
    (Path(cfg['run']) / 'workspace' / 'worker-2').mkdir(parents=True)
    for gen in (0, 3):
        argv = fleet.claude_argv(tmp_path / 'c.json', cfg, 'worker-2', gen, 'hi')
        assert argv[argv.index('--name') + 1] == 'fa1b2c3d4-worker-2'
    sub = dict(cfg, task_id='M0', root_token='a1b2c3d4')
    argv = fleet.claude_argv(tmp_path / 'c.json', sub, 'worker-2', 0, 'hi')
    assert argv[argv.index('--name') + 1] == 'fa1b2c3d4-M0-worker-2'
    assert argv[-2:] == ['--', 'hi']


def test_start_name_sets_cfg_and_title(tmp_path, monkeypatch):
    from test_pi import mock_start
    args, _ = mock_start(tmp_path, monkeypatch)
    seen = {}

    def factory(name, **kw):
        seen.update(kw, session=name)
        return SimpleNamespace(meta_path=tmp_path / 'isolated-state' / 'cmux-fleet.json', session_exists=lambda: False,
                               spawn_window=lambda *a, **k: SimpleNamespace(success=True))
    monkeypatch.setattr(cmux_manager, 'CmuxSessionManager', factory)
    fleet.main(args + ['--name', '研报'])
    cfg = fleet.load(tmp_path / 'latest.json')
    assert cfg['name'] == '研报' and cfg['agents_title'] == 'Fleet 研报' and len(cfg['token']) == 8
    assert seen == {'agents_title': 'Fleet 研报', 'session': 'cmux-fleet'}


# ------------------------------------------------------------------ real cmux_manager primitives (fake _run)
def test_manager_release_adopt_place_after_close(tmp_path, monkeypatch):
    live = {'parent-ws': {'w1': {'pane_id': 'p1'}, 'ph': {'pane_id': 'p1'}}, 'child-ws': {'cw1': {'pane_id': 'c1'}}}
    calls = []

    def run(*args):
        calls.append(args)
        if args[0] == 'move-surface':
            sid = args[args.index('--surface') + 1]
            src = next(ws for ws in live if sid in live[ws])
            live[args[args.index('--workspace') + 1]][sid] = {'pane_id': args[args.index('--pane') + 1]}
            live[src].pop(sid)
        if args[0] == 'close-surface':
            sid = args[args.index('--surface') + 1]
            for ws in live.values():
                ws.pop(sid, None)
        return SimpleNamespace(returncode=0, stdout='OK', stderr='')
    monkeypatch.setattr(cmux_manager, '_run', run)
    parent = cmux_manager.CmuxSessionManager('cmux-fleet', state_dir=tmp_path)
    child = cmux_manager.CmuxSessionManager('cmux-fleet:M0', state_dir=tmp_path)
    for m, ws in ((parent, 'parent-ws'), (child, 'child-ws')):
        monkeypatch.setattr(m, '_surfaces', lambda w, ws=ws: {s: dict(v) for s, v in live.get(w, {}).items()})
    parent._save_meta({'workspace': 'parent-ws', 'control_workspace': None,
                       'surfaces': {'w1': {'type': 'worker'}, 'ph': {'type': 'placeholder'}}})
    child._save_meta({'workspace': 'child-ws', 'control_workspace': None, 'surfaces': {'cw1': {'type': 'worker'}}})
    info = parent.release('w1')
    assert info == {'type': 'worker'} and not parent.owns('w1')
    assert child.adopt('w1', 'cw1', info) and child.owns('w1') and live['child-ws']['w1']['pane_id'] == 'c1'
    assert child.place_after('parent-ws')
    assert ('reorder-workspace', '--workspace', 'child-ws', '--after', 'parent-ws') in calls
    assert child.release('w1') and parent.adopt('w1', 'ph', {'type': 'worker'}) and parent.owns('w1')
    monkeypatch.setattr(child, 'kill_pane', lambda sid: (live['child-ws'].pop(sid, None), child.release(sid), True)[2])
    assert child.close_all() and not live['child-ws']
    assert ('close-workspace', '--workspace', 'child-ws') in calls


def test_v2_daemon_loop_runs_the_fleet_tick(tree, monkeypatch):
    """Regression: the v2 loop called RuntimeV2.tick directly, so roster reload, result.json and
    sub-fleet collection (all in Fleet.tick) never ran in a real v2 daemon."""
    t, f = tree, tree.f
    seen = []

    def once(fl):
        seen.append(fl)
        fl.stopping = True
    monkeypatch.setattr(subfleet, 'tick', once)
    f.v2.close()
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=t.sessions)
    t.fleets[0] = again
    again.v2.run()
    assert seen == [again]


def test_parent_task_end_stops_running_children(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    f.v2.finish('blocked', 'main task blocked')
    subfleet.tick(f)
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'stopping' and rec['outcome'] == 'cancelled'
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(rec['run'])['state'] == 'failed'


def test_cli_spawn_targets_the_owning_fleet(tree):
    t, f = tree, tree.f
    args = SimpleNamespace(task_id='M0.1', slot='worker-1', goal='g', workers=1, output=None)
    assert subfleet.cli_spawn(f.cfg, args, wait=0) == {'status': 'rejected', 'reason': 'no running sub-fleet M0'}
    args.task_id = 'M3'
    out = subfleet.cli_spawn(f.cfg, args, wait=0)
    assert out['status'] == 'pending' and (f.run_dir / 'subfleet-requests' / (out['request'] + '.json')).exists()
