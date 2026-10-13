"""Sub-fleet hand-over of a tmux supervisor_slot (dual display, named sub-fleet §3.1): the wiring.

Same two-Redis / in-process daemon / fake cmux world as test_subfleet.py. The world's managers gain the
dual-display primitives (cmux_manager has the real ones, tested with real tmux in test_tmux_view.py):
the parent keeps its tab of the slot, the child opens a second client tab and detaches it on return.
"""
import json
from pathlib import Path

import pytest

from test_subfleet import (WorldMgr, child_daemon, die, hook, parent_hook, spawn_m0, step,  # noqa: F401
                           stop_child_like_daemon, tree)
from cmux_fleet import fleet, subfleet

SOCK = '/tmp/fleet-run/tmux.sock'


def tmux_view_info(self, sid):
    if sid not in self.w.tmux_owner or not self.owns(sid):
        return None if sid not in self.w.views else dict(self.w.views[sid], tmux_role='view')
    return {'type': 'worker', 'label': 'fx-x', 'tmux_socket': SOCK, 'tmux_session': self.w.tmux_owner[sid]}


def attach_view(self, info, peer):
    if info.get('tmux_session') not in self.w.tmux_owner.values() or not self.owns(peer):
        return None
    sid = self._new('view')
    self.w.views[sid] = {'tmux_socket': info['tmux_socket'], 'tmux_session': info['tmux_session']}
    return sid


def detach_view(self, sid):
    if sid not in self.w.views or self.w.owner.get(sid) != self.session:
        return False
    self.w.detached.append(sid)
    self.w.views.pop(sid)
    self.w.alive.discard(sid)
    self.w.owner.pop(sid, None)
    return True


def meta(self):
    return {'surfaces': {sid: {'tmux_role': 'view'} for sid in self.w.views if self.w.owner.get(sid) == self.session}}


@pytest.fixture
def tmux_tree(tree, monkeypatch):
    t, f = tree, tree.f
    for name, fn in (('tmux_view_info', tmux_view_info), ('attach_view', attach_view),
                     ('detach_view', detach_view), ('_meta', meta)):
        monkeypatch.setattr(WorldMgr, name, fn, raising=False)
    t.world.views, t.world.detached = {}, []
    t.world.tmux_owner = {f.agent('worker-1')['pane']: 'worker-1-g0'}
    f.cfg['subfleets'] = [{'name': 'ops', 'supervisor_slot': 'worker-1'}]
    return t


def test_tmux_slot_args_only_for_a_declared_supervisor_slot(tmp_path):
    cfg = {'run': str(tmp_path), 'subfleets': [{'name': 'ops', 'supervisor_slot': 'ops-a'}, 'junk']}
    assert subfleet.tmux_spawn_args(cfg, 'ops-a', 3) == {'tmux_socket': str(tmp_path / 'tmux.sock'),
                                                          'tmux_session': 'ops-a-g3'}
    assert subfleet.tmux_spawn_args(cfg, 'ops-b', 0) == {} and subfleet.tmux_spawn_args({'run': '/x'}, 'ops-a', 0) == {}
    assert subfleet.tmux_spawn_args(dict(cfg, adopt={'pane': 'p'}), 'ops-a', 0) == {}  # never inside a sub-fleet
    assert subfleet.tmux_spawn_args(cfg, 'supervisor', 0) == {}


def test_spawn_passes_the_tmux_session_of_the_generation(tmux_tree):
    t, f = tmux_tree, tmux_tree.f
    f.setf('worker-1', generation=1)
    f.spawn('worker-1', 'READY please')
    f.spawn('worker-2', 'READY please')
    *_, w1, w2 = f.mgr.spawned
    assert w1['tmux_socket'] == str(f.run_dir / 'tmux.sock') and w1['tmux_session'] == 'worker-1-g1'
    assert 'tmux_socket' not in w2


def test_tmux_slot_is_shown_in_both_workspaces_and_comes_back_without_moving(tmux_tree):
    t, f = tmux_tree, tmux_tree.f
    sid = f.agent('worker-1')['pane']
    rec, out = spawn_m0(t)
    child_cfg = fleet.load(Path(rec['run']) / 'config.json')
    # no placeholder, nothing released: the parent keeps its own tab of the session
    assert rec['view'] == 1 and rec['placeholder'] == '' and not getattr(f.mgr, 'windows', [])
    assert t.world.owner[sid] == 'cmux-fleet' and f.mgr.owns(sid)
    info = f.agent('worker-1')
    assert info['state'] == 'delegated' and info['pane'] == sid and info['delegated_pane'] == sid
    assert child_cfg['adopt']['info']['tmux_session'] == 'worker-1-g0'
    assert json.loads(Path(t.world.mgrs['cmux-fleet:M0'].meta_path).read_text()) == {}  # child registry: nothing of the parent
    # the child opens a second client tab as its supervisor; the role change is typed there
    cf = child_daemon(t, rec)
    view = cf.agent('supervisor')['pane']
    assert view != sid and cf.mgr.owns(view) and t.world.views[view]['tmux_session'] == 'worker-1-g0'
    assert cf.mgr.arranged[0] == view
    role_change = [x for p, x in t.world.sent if p == view][-1]
    assert 'sub-fleet M0' in role_change
    for w in cf.workers:
        assert hook(t, cf, w, 'READY', prompt='startup')['status'] == 'ready'
    assert parent_hook(t, 'READY', role_change)['status'] == 'ready'  # same session, parent's worker-1 hook
    step(t)
    assert 'Research five sources' in [x for p, x in t.world.sent if p == view][-1]
    # finish: the child detaches its client; the parent tab never moved and is told to be a worker again
    inside = Path(rec['run']) / 'workspace' / 'worker-1' / 'm0.md'
    inside.parent.mkdir(parents=True, exist_ok=True)
    inside.write_text('merged')
    cf.v2.finish('completed', 'done')
    cf.write_result()
    subfleet.tick(f)
    stop_child_like_daemon(cf)
    assert t.world.detached == [view] and fleet.load(Path(rec['run']) / 'handback.json')['pane'] == sid
    subfleet.tick(f)
    rec = subfleet.record(rec['run'])
    assert rec['state'] == 'returned' and out.read_text() == 'merged'
    assert f.agent('worker-1')['pane'] == sid and f.agent('worker-1')['state'] == 'starting'
    assert 'worker of the main fleet again' in [x for p, x in t.world.sent if p == sid][-1]
    assert sid in t.world.alive and t.world.owner[sid] == 'cmux-fleet'


def test_crashed_child_collect_detaches_its_client_and_keeps_the_session(tmux_tree):
    t, f = tmux_tree, tmux_tree.f
    sid = f.agent('worker-1')['pane']
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    view = cf.agent('supervisor')['pane']
    die(cf)  # no hand-back: its client tab is still open
    assert subfleet.stop_children(f, wait=1)
    assert t.world.detached == [view] and view not in t.world.alive
    assert f.agent('worker-1')['pane'] == sid and sid in t.world.alive
    assert all(f.v2.retire(s, 'explicit fleet stop') for s in f.names)  # the parent's stop retires its tab
    assert sid not in t.world.alive


def test_lost_session_respawns_the_slot_in_its_own_place(tmux_tree):
    t, f = tmux_tree, tmux_tree.f
    sid = f.agent('worker-1')['pane']
    rec, _ = spawn_m0(t)
    cf = child_daemon(t, rec)
    die(cf)
    t.world.alive.discard(sid)  # the session died with the child (tab gone)
    assert subfleet.stop_children(f, wait=1)
    info = f.agent('worker-1')
    assert info['generation'] == '1' and info['pane'] != sid and f.mgr.spawned[-1]['tmux_session'] == 'worker-1-g1'


def test_failed_child_start_rolls_back_without_touching_the_parent_tab(tmux_tree, monkeypatch):
    t, f = tmux_tree, tmux_tree.f
    sid = f.agent('worker-1')['pane']

    def boom(*a, **k):
        raise RuntimeError('no redis')
    monkeypatch.setattr(fleet, 'start_redis', boom)
    with pytest.raises(subfleet.Refused):
        spawn_m0(t)
    assert f.agent('worker-1')['state'] == 'idle' and f.agent('worker-1')['pane'] == sid
    assert t.world.owner[sid] == 'cmux-fleet' and sid in t.world.alive
