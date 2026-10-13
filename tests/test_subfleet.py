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

from test_fleet import codex_rows, received, write_rollout
import hook_sim  # noqa: E402
from cmux_fleet import cmux_manager, fleet, redis_runtime, roster, runtime_v2, subfleet


# ------------------------------------------------------------------ fake cmux world
class World:
    def __init__(self, state_dir):
        self.n, self.alive, self.owner, self.ws_of = 0, set(), {}, {}
        self.sent, self.keys, self.mgrs, self.order, self.closed = [], [], {}, [], []
        self.state_dir = Path(state_dir)
        self.urls, self.selected, self.select_ok, self.url_reads = {}, [], True, 0

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
        self.w.sent.append((pane, '<key ' + k + '>'))  # one ordered log of what reached each tab
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
        self.__dict__.setdefault('reloaded', []).append(pane)
        return True

    def browser_url(self, pane):
        self.w.url_reads += 1
        return self.w.urls.get(pane) if self.owns(pane) else None

    def browser_goto(self, pane, url):
        if not self.owns(pane):
            return False
        self.w.urls[pane] = url
        return True

    def select_workspace(self, ws):
        self.w.selected.append(ws)
        return self.w.select_ok

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


def given(t, f, slot):
    """What the slot's current generation was given: queued for its Codex session (codex queue), typed
    into its pane (a role change, a non-hook Claude slot), or handed over by its Stop hook (a Claude
    worker's deliveries)."""
    return received(t.world, f.agent(slot)['pane']) + hook_sim.handed_to(f, slot)


def hook(t, f, slot, text, *, prompt=None, thread=None, generation=None):
    """Fire the real turn hook the CLI in ``slot`` of fleet ``f`` was started with."""
    gen = int(f.agent(slot)['generation']) if generation is None else generation
    thread = thread or t.threads.setdefault((str(f.run_dir), slot, gen), str(uuid.uuid4()))
    if prompt is None:
        prompt = given(t, f, slot)[-1]
    if slot == 'supervisor' and fleet.agent_cli(f.cfg, slot) == 'codex':
        turn_id, rows = codex_rows(thread, prompt, text)  # the queued delivery's own turn, or a typed one
        write_rollout(t.sessions, thread, fleet.agent_cwd(f.cfg, slot), rows=rows)
        payload = json.dumps({'type': 'agent-turn-complete', 'thread-id': thread, 'turn-id': turn_id,
                              'input-messages': [prompt], 'last-assistant-message': text})
        return drive(t, lambda: fleet.notify(f.path, slot, str(gen), payload, t.sessions))
    return claude_turn(t, f.path, slot, gen, thread, prompt, text)


def claude_turn(t, cfg_path, slot, gen, thread, prompt, text):
    transcript = t.claude / (thread + '.jsonl')
    transcript.parent.mkdir(parents=True, exist_ok=True)
    rows = [hook_sim.user_row(prompt, hook_sim.was_handed(slot, prompt)),
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': text}]}}]
    with transcript.open('a') as fh:
        fh.write('\n'.join(json.dumps(x) for x in rows) + '\n')
    raw = json.dumps({'session_id': thread, 'transcript_path': str(transcript), 'last_assistant_message': text})
    return drive(t, lambda: hook_sim.claude_stop(cfg_path, slot, str(gen), raw))


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
@pytest.mark.parametrize('path', ['hook', 'typed_fallback'])
def test_worker_pane_becomes_subfleet_supervisor_and_comes_back(tree, path):
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
    role_change = received(t.world, sid)[-1]
    assert 'sub-fleet M0' in role_change and 'READY' in role_change
    for w in cf.workers:
        assert hook(t, cf, w, 'READY', prompt='startup')['status'] == 'ready'
    # READY of the adopted pane goes through the PARENT worker-1 hook and lands in the child
    assert parent_hook(t, 'READY', role_change)['status'] == 'ready'
    assert cf.agent('supervisor')['runtime_ready'] == '1'  # READY, then the task is delivered at once
    assert cf.agent('supervisor')['thread'] == t.threads[(str(f.run_dir), 'worker-1', 0)]  # same session
    assert f.agent('worker-1')['state'] == 'delegated'
    step(t)
    task_msg = received(t.world, sid)[-1]
    assert 'Research five sources and merge them' in task_msg
    # §26: the child writes the deliverable inside its own workspace, never the parent's path
    inside = Path(rec['run']) / 'workspace' / 'worker-1' / 'm0.md'
    assert rec['deliver'] == [[str(inside), str(out)]] and str(inside) in task_msg and str(out) not in task_msg
    decision = json.dumps({'status': 'RUNNING', 'completion': 10, 'reason': 'split', 'dispatch': [
        {'to': w, 'instruction': 'Write ' + w + '/part.md with two sources each'} for w in cf.workers]})
    assert parent_hook(t, decision, task_msg)['status'] == 'committed'
    step(t)
    for w in cf.workers:
        assert any('part.md' in x for x in given(t, cf, w))
    assert not any('part.md' in x for x in given(t, f, 'worker-2'))
    # the sub-fleet finishes: deliverable written, task completed, result.json written
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('merged findings')
    cf.v2.finish('completed', 'all parts merged')
    cf.write_result()
    subfleet.tick(f)  # parent sees result.json and asks the child to stop
    assert subfleet.record(rec['run'])['state'] == 'stopping'
    stop_child_like_daemon(cf)
    assert fleet.load(Path(rec['run']) / 'handback.json')['pane'] == sid
    assert sid in t.world.alive  # the adopted pane was released, never killed
    if path == 'typed_fallback':
        # the documented non-hook path keeps the typed return (daemon and hooks read the same config)
        f.cfg['claude_hook_delivery'] = False
        fleet.save(f.path, dict(fleet.load(f.path), claude_hook_delivery=False))
    # Baseline BEFORE the return: everything typed / keyed so far (take-over role change included).
    sent_before, keys_before = list(t.world.sent), list(t.world.keys)
    gen_before = int(f.agent('worker-1')['generation'])
    subfleet.tick(f)  # parent collects: pane back in place of the placeholder, child tabs closed
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'returned' and rec['outcome'] == 'completed'
    assert out.read_text() == 'merged findings'  # the parent daemon copied it back before checking
    assert rec['placeholder'] not in t.world.alive
    assert 'cmux-fleet:M0' in t.world.closed and redis_down(child_cfg)
    assert (fleet.agent_cwd(f.cfg, 'worker-1') / 'CLAUDE.md').read_text() == worker_rules
    assert 'cmux-fleet:M0' not in [p.stem for p in t.world.state_dir.glob('*.json')]
    info = f.agent('worker-1')
    assert not info['delegate_cfg']
    new_typed = [(p, x) for p, x in t.world.sent[len(sent_before):]]
    new_keys = [(p, k) for p, k in t.world.keys[len(keys_before):]]
    report = 'Done:\n- wrote after.md\nEvidence:\n- worker-1/after.md\nNot done: none\nBlocked: none'
    if path == 'hook':
        # R1-01: a hook-channel Claude worker is never typed into after its launch prompt. The
        # returned session is retired and the next generation starts from its CLI-argument prompt.
        panes = {sid, info['pane']}
        assert info['pane'] != sid and int(info['generation']) == gen_before + 1
        assert info['state'] == 'starting'
        assert not [x for p, x in new_typed if p in panes], 'the return typed into a hook worker pane'
        assert not [k for p, k in new_keys if p in panes and k == 'Enter'], 'Enter was sent to a hook worker pane'
        assert sid not in t.world.alive or t.world.owner.get(sid) != 'cmux-fleet:M0'
        # the new generation becomes READY through its own Stop hook, then work reaches it through the hook
        assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
        assert f.agent('worker-1')['state'] == 'idle'
        f.queue('worker-1', '[Supervisor instruction ' + f.task()['id'] + '] Write worker-1/after.md now')
        step(t)
        after = given(t, f, 'worker-1')[-1]
        assert 'worker-1/after.md' in after and after in hook_sim.handed_to(f, 'worker-1')
        assert not [x for p, x in t.world.sent[len(sent_before):] if p in panes | {f.agent('worker-1')['pane']}]
        assert hook(t, f, 'worker-1', report, prompt=after)['status'] == 'committed'
        notice = [f.v2.bus.message(m)['body']['text'] for m in f.v2.bus.pending('supervisor')
                  + [f.v2.bus.slot_state('supervisor').get('message_id') or 'x'] if f.v2.bus.message(m)]
        assert any('[Sub-fleet M0 completed]' in x and 'new generation' in x for x in notice)
        # whole run (before, during and after the delegation): no parent-fleet business delivery was
        # ever typed into any pane worker-1 had; only the sub-fleet role change / child supervisor
        # traffic reached the adopted pane, and that is the child's own (typed) supervisor path
        parent_marker = 'Shared output directory: ' + str(f.workspace())
        all_panes = panes | {f.agent('worker-1')['pane']}
        assert not [x for p, x in t.world.sent if p in all_panes and (parent_marker in x or '[Supervisor instruction' in x)]
        assert any('worker-1/after.md' in x for x in hook_sim.texts('worker-1')), 'parent work reached worker-1 through the hook'
    else:
        # control: without the hook channel the returned session keeps its generation and is
        # told by a typed role-change prompt (the pre-hook behaviour)
        assert t.world.owner[sid] == 'cmux-fleet' and info['pane'] == sid and info['state'] == 'starting'
        back = [x for p, x in new_typed if p == sid][-1]
        assert 'worker of the main fleet again' in back
        assert parent_hook(t, 'READY', back)['status'] == 'ready'  # same generation, same session
        assert f.agent('worker-1')['state'] == 'idle' and int(f.agent('worker-1')['generation']) == gen_before
        assert any('[Sub-fleet M0 completed]' in f.v2.bus.message(m)['body']['text']
                   for m in f.v2.bus.pending('supervisor') + [f.v2.bus.slot_state('supervisor').get('message_id') or 'x']
                   if f.v2.bus.message(m))
        f.queue('worker-1', '[Supervisor instruction ' + f.task()['id'] + '] Write worker-1/after.md now')
        step(t)
        after = [x for p, x in t.world.sent if p == sid][-1]
        assert 'worker-1/after.md' in after and after not in hook_sim.handed_to(f, 'worker-1')
        assert parent_hook(t, report, after)['status'] == 'committed'


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
    # hook-channel worker: the returned session is replaced by its next generation (never typed into)
    assert f.agent('worker-1')['generation'] == '1' and f.agent('worker-1')['pane'] != rec['pane']
    assert not [x for p, x in t.world.sent if p == rec['pane'] and 'worker of the main fleet again' in x]
    assert hook(t, f, 'worker-1', 'READY', prompt='startup')['status'] == 'ready'
    rec2, _ = spawn_m0(t)  # a retry keeps the old run (history) and counts the attempt
    assert rec2['attempt'] == 2 and rec2['run'].endswith('M0-attempt-2')


def page_data(run):
    from cmux_fleet import history
    page = history.page_path(run).read_text()
    return json.loads(page.split('const DATA=', 1)[1].split(';\n', 1)[0])


def test_history_pages_link_parent_and_children_both_ways(tree):
    """Design §19 / nested §5.3: parent page lists its sub-fleets (jump), child page links back."""
    from cmux_fleet import history
    t, f = tree, tree.f
    f.refresh_history()
    assert page_data(f.run_dir)['children'] == [] and page_data(f.run_dir)['parent'] is None
    reloads = len(f.mgr.reloaded)
    rec, out = spawn_m0(t)
    data = page_data(f.run_dir)  # the parent page was rewritten (and reloaded) on spawn
    assert len(f.mgr.reloaded) > reloads
    [kid] = data['children']
    assert kid['task_id'] == 'M0' and kid['state'] == 'running' and kid['slot'] == 'worker-1'
    assert kid['url'] == history.page_url(rec['run']) and kid['title'] == '研报 › worker-1 › 子 fleet M0'
    cf = child_daemon(t, rec)
    child = page_data(rec['run'])
    assert child['parent']['url'] == history.page_url(f.run_dir) and child['children'] == []
    assert child['parent']['title'] == (f.cfg.get('agents_title') or 'Fleet agents')
    page = history.page_path(rec['run']).read_text()
    assert 'id="fleets"' in page and '回到父 fleet' in page
    # finished: the parent page shows the outcome without a manual refresh
    out.write_text('done')
    cf.v2.finish('completed', 'ok')
    cf.write_result()
    subfleet.tick(f)
    assert page_data(f.run_dir)['children'][0]['state'] == 'stopping'
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    [kid] = page_data(f.run_dir)['children']
    assert kid['state'] == 'returned' and kid['outcome'] == 'completed'


def test_history_page_link_text_is_escaped(tmp_path):
    from cmux_fleet import history
    links = {'parent': {'task_id': 'M0', 'url': 'file:///x', 'title': '"><img src=x onerror=alert(1)>'},
             'children': [{'task_id': '</script><b>', 'url': 'file:///y', 'title': 't', 'state': 'running'}]}
    page = history.write_page(tmp_path, [], links).read_text()
    assert page.count('</script>') == 1
    assert page_data(tmp_path)['parent']['title'] == '"><img src=x onerror=alert(1)>'


def base36(n):
    digits = ''
    while True:
        n, r = divmod(n, 36)
        digits = '0123456789abcdefghijklmnopqrstuvwxyz'[r] + digits
        if not n:
            return digits


def click(url, rel, at=None, shown='file:///shown/history.html#agent=worker-1'):
    """The URL a page link click leaves in the pane (history.py): the shown page with an explicit marker."""
    from urllib.parse import urlencode
    ms = int((time.time() if at is None else at) * 1000)
    base, _, hash_ = shown.partition('#')
    return base + '?' + urlencode({subfleet.JUMP_PARAM: base36(ms) + '-' + uuid.uuid4().hex[:6], 'rel': rel,
                                   'to': url}) + ('#' + hash_ if hash_ else '')


def history_pane(f):
    return f.r.hget(fleet.SERVICE, 'history_pane')


def jump(f, url, now=None):
    t = time.time() if now is None else now
    f.jump_next = 0  # tests drive the poll interval explicitly
    f.world_url(url)
    return subfleet.follow_jump(f, t)


@pytest.fixture
def linked(tree):
    """Parent with running sub-fleet M0 and its in-process child daemon; both history panes live."""
    from cmux_fleet import history
    t, f = tree, tree.f
    rec, out = spawn_m0(t)
    cf = child_daemon(t, rec)
    for x in (f, cf):
        x.world_url = lambda url, x=x: t.world.urls.__setitem__(history_pane(x), url)
        t.world.urls[history_pane(x)] = history.page_url(x.run_dir)
    t.cf, t.rec, t.out = cf, rec, out
    t.ws_parent, t.ws_child = f.mgr.workspace_id(), cf.mgr.workspace_id()
    assert t.ws_parent and t.ws_child and t.ws_parent != t.ws_child
    return t


def test_clicked_page_links_switch_workspaces_both_ways_and_restore_the_source_page(linked):
    """Option (a): a link click (explicit marker) -> select-workspace of the matching fleet; the
    clicking pane goes back to its own page (last seen state), the target pane is not touched."""
    from cmux_fleet import history
    t, f, cf = linked, linked.f, linked.cf
    own, kid = history.page_url(f.run_dir), history.page_url(t.rec['run'])
    assert jump(f, own + '#agent=worker-2&gen=&view=note&q=') is None  # ordinary state: remembered only
    assert t.world.selected == []
    assert jump(f, click(kid, 'child')) == 'jump'
    assert t.world.selected == [t.ws_child]
    assert t.world.urls[history_pane(f)] == own + '#agent=worker-2&gen=&view=note&q='
    assert t.world.urls[history_pane(cf)] == history.page_url(cf.run_dir)  # the child's pane: untouched
    # clicked right after changing state on our own page: the marker URL's own hash wins over the last poll
    assert jump(f, click(kid, 'child', shown=own + '#agent=supervisor&view=note')) == 'jump'
    assert t.world.urls[history_pane(f)] == own + '#agent=supervisor&view=note'
    t.world.selected.pop()
    assert jump(cf, click(own, 'parent')) == 'jump'
    assert t.world.selected == [t.ws_child, t.ws_parent]
    assert t.world.urls[history_pane(cf)] == history.page_url(cf.run_dir)


def test_reload_repeated_tick_and_daemon_restart_never_switch_twice(linked):
    from cmux_fleet import history
    t, f = linked, linked.f
    own, kid = history.page_url(f.run_dir), history.page_url(t.rec['run'])
    url = click(kid, 'child')
    assert jump(f, url) == 'jump'
    # the goto back did not land (or the page reloaded the marker URL): the same click is not a new one
    assert jump(f, url) == 'repeat' and t.world.urls[history_pane(f)] == own
    restarted = fleet.Fleet(f.path, f.r, f.mgr, sessions=t.sessions)  # a new daemon on the same Redis
    restarted.world_url = f.world_url
    assert jump(restarted, url) == 'repeat'
    assert t.world.selected == [t.ws_child]
    # a plain reload of the marker-free page is never a click
    t.world.urls[history_pane(f)] = kid
    assert jump(f, kid) is None and t.world.selected == [t.ws_child]
    # a click left over from before a long daemon outage only puts the page back
    assert jump(f, click(kid, 'child', at=time.time() - 600)) == 'stale'
    assert t.world.selected == [t.ws_child] and t.world.urls[history_pane(f)] == own
    # the poll interval: one URL read per JUMP_POLL, not one per loop pass
    reads = t.world.url_reads
    f.jump_next = 0
    now = time.time()
    subfleet.follow_jump(f, now); subfleet.follow_jump(f, now + .1); subfleet.follow_jump(f, now + .2)
    assert t.world.url_reads == reads + 1


def test_only_registered_matching_relations_jump(linked, tmp_path):
    from cmux_fleet import history
    t, f, cf = linked, linked.f, linked.cf
    own, kid = history.page_url(f.run_dir), history.page_url(t.rec['run'])
    assert jump(f, click(kid, 'parent')) == 'unrelated'  # right page, wrong relation
    assert jump(cf, click(own, 'child')) == 'unrelated'  # the parent is not a child of the child
    other = tmp_path / 'other-fleet'
    other.mkdir()
    assert jump(f, click(history.page_url(other), 'child')) == 'unrelated'  # not ours
    assert jump(f, click(own, 'parent')) == 'self'  # a page link to this fleet itself
    assert jump(f, click('javascript:alert(1)', 'child')) == 'stale'  # only file pages are targets
    assert jump(f, own + '?fleet-jump=zz&rel=child') == 'stale'  # malformed marker: page back, no switch
    assert t.world.selected == []
    assert t.world.urls[history_pane(f)] == own


def test_failed_switch_and_missing_registration_show_the_target_page_in_place(linked):
    from cmux_fleet import history
    t, f = linked, linked.f
    kid = history.page_url(t.rec['run'])
    t.world.select_ok = False
    assert jump(f, click(kid, 'child')) == 'failed'
    assert t.world.urls[history_pane(f)] == kid  # what was clicked, without the marker
    assert t.world.selected == [t.ws_child]
    t.world.select_ok = True
    assert jump(f, t.world.urls[history_pane(f)]) is None  # no marker any more: no retry storm
    t.cf.mgr.ws = None  # the child's cmux registry lost its workspace
    assert jump(f, click(kid, 'child')) == 'unregistered'
    assert t.world.selected == [t.ws_child] and t.world.urls[history_pane(f)] == kid


def test_returned_subfleet_keeps_a_readable_page_and_never_moves_the_view(linked):
    from cmux_fleet import history
    t, f, cf = linked, linked.f, linked.cf
    kid = history.page_url(t.rec['run'])
    t.out.write_text('done')
    cf.v2.finish('completed', 'ok')
    cf.write_result()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    subfleet.tick(f)
    assert subfleet.record(t.rec['run'])['state'] == 'returned'
    assert jump(f, click(kid, 'child')) == 'closed'
    assert t.world.selected == [] and t.world.urls[history_pane(f)] == kid
    assert history.page_path(t.rec['run']).is_file()
    # from the closed child's page (shown in the parent's pane) 'back to the parent' is this fleet
    assert jump(f, click(history.page_url(f.run_dir), 'parent')) == 'self'
    assert t.world.selected == [] and t.world.urls[history_pane(f)].startswith(history.page_url(f.run_dir))


def test_the_daemon_tick_follows_a_click(linked):
    from cmux_fleet import history
    t, f = linked, linked.f
    f.jump_next = 0
    f.world_url(click(history.page_url(t.rec['run']), 'child'))
    f.tick()
    assert t.world.selected == [t.ws_child] and t.world.urls[history_pane(f)] == history.page_url(f.run_dir)


def test_manager_browser_url_goto_and_select_workspace(tmp_path, monkeypatch):
    calls = []

    def run(*args):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='file:///r/history.html?fleet-jump=x\n', stderr='')
    monkeypatch.setattr(cmux_manager, '_run', run)
    m = cmux_manager.CmuxSessionManager('cmux-fleet', state_dir=tmp_path)
    monkeypatch.setattr(m, '_surfaces', lambda w: {'h1': {'pane_id': 'p1'}} if w == 'ws' else {})
    m._save_meta({'workspace': 'ws', 'control_workspace': None, 'surfaces': {'h1': {'type': 'history'}}})
    assert m.browser_url('h1') == 'file:///r/history.html?fleet-jump=x'
    assert m.browser_url('other') is None  # never reads a surface it does not own
    assert m.browser_goto('h1', 'file:///r/history.html') and m.select_workspace('ws-2')
    assert calls == [('browser', '--surface', 'h1', 'url'), ('browser', '--surface', 'h1', 'goto', 'file:///r/history.html'),
                     ('select-workspace', '--workspace', 'ws-2')]
    assert not m.select_workspace('')


def test_fleets_without_links_never_read_the_pane(tree):
    f = tree.f
    f.jump_next = 0
    assert subfleet.follow_jump(f) is None and tree.world.url_reads == 0


def test_page_links_carry_the_explicit_jump_marker(tmp_path):
    from cmux_fleet import history
    links = {'parent': {'task_id': '', 'url': 'file:///p/history.html', 'title': 'P'},
             'children': [{'task_id': 'M0', 'url': 'file:///c/history.html', 'title': 'C', 'state': 'failed',
                           'outcome': 'failed'}]}
    page = history.write_page(tmp_path, [], links).read_text()
    assert "'fleet-jump':" in page and 'data-rel' in page and subfleet.JUMP_PARAM == 'fleet-jump'
    assert "location.href.split(/[?#]/)[0]+'?'" in page  # the marker stays on the shown file
    assert "c.outcome!==c.state" in page  # 'failed (failed)' is shown once


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
    # taken back from the child: a hook-channel worker's returned session is retired at once (its
    # next generation replaces it), so no orphan stays with the child; the parent's stop retires the rest
    assert sid not in t.world.alive or t.world.owner.get(sid) == 'cmux-fleet'
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
    role_change = received(t.world, rec['pane'])[-1]
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
    # claude+claude/task is verified by a real smoke now; service (unlisted) still needs adopt-free refusal.
    base = dict(transport_version=2, allow_development_v2=True, supervisor_cli='claude', worker_cli='claude')
    assert runtime_v2.v2_cli_problem(base) is None
    assert runtime_v2.v2_cli_problem(dict(base, mode='service'))
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


def test_parent_resume_keeps_a_delegated_slot_and_relaunches_the_child(tree):
    t, f = tree, tree.f
    rec, _ = spawn_m0(t)
    f.v2.close()
    again = fleet.Fleet(f.path, f.r, f.mgr, sessions=t.sessions)
    t.fleets[0] = again
    t.f = again
    again.resume_requested = True
    again.v2.resume_run()
    info = again.agent('worker-1')
    assert info['state'] == 'delegated' and info['generation'] == '0' and info['pane'] == rec['placeholder']
    assert rec['placeholder'] in t.world.alive and t.world.owner[rec['pane']] == 'cmux-fleet:M0'
    assert subfleet.delegated_route(again.cfg, 'worker-1', 0)[0]['task_id'] == 'M0'
    child_mgr = t.world.mgrs['cmux-fleet:M0']
    assert any('--resume' in cmd for _, cmd in getattr(child_mgr, 'windows', []))  # child daemon relaunched
    assert again.agent('worker-2')['state'] != 'delegated'


def test_failed_child_redis_start_rolls_back_without_killing_the_parent(tree, monkeypatch):
    t, f = tree, tree.f
    sid = f.agent('worker-1')['pane']

    def boom(cfg, tries=30):
        raise SystemExit('private Redis did not become ready')
    monkeypatch.setattr(fleet, 'start_redis', boom)
    with pytest.raises(subfleet.Refused, match='did not become ready'):
        spawn_m0(t)
    info = f.agent('worker-1')
    assert info['state'] == 'idle' and info['pane'] == sid and t.world.owner[sid] == 'cmux-fleet'
    assert not subfleet.active_children(f.run_dir)
    assert subfleet.record(f.run_dir / 'sub' / 'M0')['state'] == 'failed'
    assert len([p for p in t.world.alive if t.world.owner.get(p) == 'cmux-fleet']) == 4  # sup, 2 workers, history


@pytest.mark.parametrize('path', ['hook', 'typed_fallback'])
def test_return_aborts_a_running_turn_before_the_return_prompt(tree, path):
    t, f = tree, tree.f
    if path == 'typed_fallback':
        f.cfg['claude_hook_delivery'] = False
        fleet.save(f.path, dict(fleet.load(f.path), claude_hook_delivery=False))
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    f.v2.finish('blocked', 'main task blocked')
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    before = len(t.world.sent)
    subfleet.tick(f)
    log = [x for p, x in t.world.sent[before:] if p == rec['pane']]
    if path == 'hook':
        # no return prompt at all: the possibly still running session is killed, not typed into
        assert log == [] and rec['pane'] not in t.world.alive
        assert f.agent('worker-1')['generation'] == '1'
    else:
        assert log[-2] == '<key Escape>' and 'worker of the main fleet again' in log[-1]


def test_lost_session_is_replaced_by_the_next_generation_in_place(tree):
    t, f = tree, tree.f
    rec, out = spawn_m0(t)
    cf = child_daemon(t, rec)
    out.write_text('done')
    cf.v2.finish('completed', 'ok')
    cf.write_result()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    (Path(rec['run']) / 'handback.json').unlink()
    t.world.alive.discard(rec['pane'])  # e.g. cmux restarted: the delegated session is gone
    t.world.owner.pop(rec['pane'], None)
    subfleet.tick(f)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['label'] == 'worker-1.1' and info['state'] == 'starting'
    assert rec['placeholder'] not in t.world.alive and info['pane'] not in (rec['pane'], rec['placeholder'])
    assert subfleet.record(rec['run'])['state'] == 'returned'
