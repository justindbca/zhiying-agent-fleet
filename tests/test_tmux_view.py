"""Dual display of a sub-fleet's supervisor_slot (docs/design.md §19 / named sub-fleet §3.1): real tmux.

The CLI runs in the fleet's own tmux server (private socket, the fleet's minimal config); cmux tabs are
only its clients. cmux is faked, but each fake tab runs its command in a real pty, so the tmux clients,
``detach-client``, ``kill-session`` and the tab ending by itself (``exec``) are all real. The fake CLI is
a symlink to ``sleep`` named ``claude`` so ``has_cli_process`` sees the name the fleet looks for.
"""
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import threading
import time
import uuid
from types import SimpleNamespace

import pytest

from cmux_fleet import cmux_manager as cm

TMUX = shutil.which('tmux') or '/opt/homebrew/bin/tmux'
pytestmark = pytest.mark.skipif(not Path(TMUX).exists(), reason='tmux unavailable')


class Tab:
    def __init__(self, command):
        master, slave = pty.openpty()
        self.tty = os.path.basename(os.ttyname(slave))
        env = dict(os.environ, TERM='xterm-256color')
        env.pop('TMUX', None)
        self.proc = subprocess.Popen(['/bin/bash', '-c', command], stdin=slave, stdout=slave, stderr=slave,
                                     env=env, start_new_session=True)
        os.close(slave)
        self.master = master
        threading.Thread(target=self.drain, daemon=True).start()  # a client blocks on a full pty

    def drain(self):
        try:
            while os.read(self.master, 65536):
                pass
        except OSError:
            pass

    def alive(self):
        return self.proc.poll() is None

    def kill(self):
        if self.alive():
            self.proc.kill()
            self.proc.wait(5)


class FakeCmux:
    """Workspaces of tabs; a tab disappears when its process exits (as a cmux tab after ``exec``)."""

    def __init__(self):
        self.ws, self.n, self.calls = {}, 0, []

    def tree(self, ws):
        panes = {}
        for sid, tab in self.ws.get(ws, {}).items():
            if tab.alive():
                panes.setdefault(tab.pane, []).append({'id': sid, 'pane_id': tab.pane, 'tty': tab.tty, 'title': ''})
        return {'windows': [{'workspaces': [{'id': ws, 'title': getattr(self, 'title_' + ws, ''),
                                             'panes': [{'surfaces': s} for s in panes.values()]}]}]}

    def new_tab(self, ws, command, pane=None):
        self.n += 1
        sid = 'sf-' + str(self.n)
        tab = Tab(command or 'exec sleep 600')
        tab.pane = pane or 'pane-' + sid
        self.ws.setdefault(ws, {})[sid] = tab
        return sid

    def __call__(self, *args):
        self.calls.append(args)
        a = [x for x in args if x not in ('--json', '--id-format', 'uuids')]
        opt = lambda k: a[a.index(k) + 1] if k in a else None
        ok = lambda out='OK': SimpleNamespace(returncode=0, stdout=out, stderr='')
        cmd = a[0]
        if cmd == 'tree':
            ws = opt('--workspace')
            return ok(json.dumps(self.tree(ws))) if ws in self.ws else SimpleNamespace(returncode=1, stdout='', stderr='')
        if cmd == 'new-surface':
            sid = self.new_tab(opt('--workspace'), opt('--command'), pane=opt('--pane'))
            return ok('OK ' + sid)
        if cmd == 'new-workspace':
            ws = 'ws-' + uuid.uuid4().hex[:6]
            setattr(self, 'title_' + ws, opt('--name'))
            self.new_tab(ws, opt('--command'))
            return ok('OK ' + ws)
        if cmd == 'move-surface':
            for tabs in self.ws.values():
                if opt('--surface') in tabs:
                    tabs[opt('--surface')].pane = opt('--pane')
            return ok()
        if cmd == 'close-surface':
            tab = self.ws.get(opt('--workspace'), {}).get(opt('--surface'))
            if tab is not None:
                tab.kill()
            return ok()
        if cmd == 'current-workspace':
            return ok('')
        return ok()  # rename-tab, select-workspace, focus-panel, close-workspace, send-key ...

    def tabs(self):
        return [tab for tabs in self.ws.values() for tab in tabs.values()]


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    # A user config that must never be read: a short history and a marker option.
    (home / '.tmux.conf').write_text('set -g history-limit 7\nset -g @poisoned 1\n')
    tmux_tmp = tmp_path / 'tmux-default'
    tmux_tmp.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('TMUX_TMPDIR', str(tmux_tmp))  # where a default server's socket would appear
    monkeypatch.setenv('SHELL', '/bin/sh')
    root = Path('/tmp/cft-' + uuid.uuid4().hex[:8])  # short: macOS Unix socket path limit
    run = root / 'run'
    run.mkdir(parents=True)
    bin_dir = tmp_path / 'bin' / uuid.uuid4().hex[:8]
    bin_dir.mkdir(parents=True)
    claude = bin_dir / 'claude'
    claude.symlink_to('/bin/sleep')  # a copied system binary is killed by code signing
    fake = FakeCmux()
    monkeypatch.setattr(cm, '_run', fake)
    monkeypatch.setattr(cm, 'TMUX', TMUX)
    w = SimpleNamespace(fake=fake, run=run, sock=str(run / 'tmux.sock'), claude=str(claude), tmux_tmp=tmux_tmp,
                        parent=cm.CmuxSessionManager('cmux-fleet', state_dir=tmp_path / 'state'),
                        child=cm.CmuxSessionManager('cmux-fleet:ops', state_dir=tmp_path / 'state'))
    yield w
    subprocess.run([TMUX, '-S', w.sock, 'kill-server'], capture_output=True)
    for tab in fake.tabs():
        tab.kill()
    subprocess.run(['pkill', '-f', str(claude)], capture_output=True)  # only this test's own fake CLI
    shutil.rmtree(root, ignore_errors=True)


def tmux(w, *args):
    return subprocess.run([TMUX, '-S', w.sock, *args], capture_output=True, text=True)


def spawn(w, slot='ops-a', generation=0, lines=0):
    out = w.run / 'prompts' / (slot + '-g' + str(generation) + '.terminal.log')
    out.parent.mkdir(parents=True, exist_ok=True)
    cli = (('seq 1 ' + str(lines) + '; ') if lines else '') + w.claude + ' 600'
    res = w.parent.spawn_interactive_cli(window_name='fx-' + slot, cli_command=cli, output_file=str(out),
                                         start_directory=str(w.run), auto_close=False,
                                         tmux_socket=w.sock, tmux_session=cm.tmux_session_name(slot, generation))
    assert res.success, res.error
    return res.pane_id, out


def clients(w, session):
    p = tmux(w, 'list-clients', '-t', '=' + session, '-F', '#{client_tty}')
    return p.stdout.split() if p.returncode == 0 else []


def claude_pids(w):
    """This test's fake CLI processes (the bash wrapper around it does not count)."""
    p = subprocess.run(['ps', '-axo', 'pid=,comm='], capture_output=True, text=True)
    return [line.split(None, 1)[0] for line in p.stdout.splitlines() if line.split(None, 1)[-1].strip() == w.claude]


def wait(check, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            return False
        time.sleep(.05)
    return True


def test_session_name_never_has_a_tmux_separator():
    assert cm.tmux_session_name('tgog-ops', 3) == 'tgog-ops-g3'
    assert cm.tmux_session_name('ops.a:1', 0) == 'ops_a_1-g0'


def test_supervisor_slot_runs_in_the_fleets_tmux_and_shows_in_two_workspaces(world):
    w = world
    sid, _ = spawn(w)
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    # the CLI hangs under the tmux server, not under the tab; liveness follows the tmux pane
    assert wait(lambda: w.parent.has_cli_process(sid, ('claude',)))
    assert claude_pids(w)
    assert w.parent.process_states(sid)
    assert w.parent.process_cpu(sid) is not None  # §24 stall window: the tmux pane's process tree
    # the tab only runs the client, exec'd: nothing of the CLI is on its tty
    tab = next(t for t in w.fake.tabs() if t.alive())
    ps = subprocess.run(['ps', '-t', tab.tty, '-o', 'comm='], capture_output=True, text=True).stdout
    assert 'claude' not in ps and 'tmux' in ps and 'bash' not in ps
    # the fleet's own minimal config; the user's ~/.tmux.conf is never read; no default server started
    assert tmux(w, 'show-options', '-gv', 'history-limit').stdout.strip() == '50000'
    assert tmux(w, 'show-options', '-gv', 'window-size').stdout.strip() == 'latest'
    assert '@poisoned' not in tmux(w, 'show-options', '-g').stdout
    assert not list(w.tmux_tmp.rglob('default'))
    # delegation: the sub-fleet opens a second client in its own workspace, the parent tab stays
    peer = w.child.spawn_window('worker-1', 'sleep 600', metadata={'type': 'worker'}, start_directory=str(w.run))
    assert peer.success
    info = w.parent.tmux_view_info(sid)
    assert info['tmux_session'] == 'ops-a-g0' and info['tmux_socket'] == w.sock
    view = w.child.attach_view(info, peer.pane_id)
    assert view and view != sid and w.child.owns(view) and w.parent.owns(sid)
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 2)
    assert w.child.has_cli_process(view, ('claude',))
    # return: the child's client is detached, its tab ends by itself; the session and parent tab live on
    assert w.child.detach_view(view)
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    assert not w.child.owns(view) and view not in w.child._meta()['surfaces']
    assert w.parent.has_cli_process(sid, ('claude',)) and 'ops-a-g0' in cm.tmux_sessions(w.sock)
    assert not any('--force' in c for c in w.fake.calls)  # never a forced close (dialogs are the user's)


def test_closing_the_childs_tabs_never_ends_the_session(world):
    w = world
    sid, _ = spawn(w)
    peer = w.child.spawn_window('worker-1', 'sleep 600', metadata={'type': 'worker'}, start_directory=str(w.run))
    view = w.child.attach_view(w.parent.tmux_view_info(sid), peer.pane_id)
    assert view and wait(lambda: len(clients(w, 'ops-a-g0')) == 2)
    assert w.child.close_all()  # a sub-fleet closing its workspace (or a crashed child's tabs)
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    assert 'ops-a-g0' in cm.tmux_sessions(w.sock) and w.parent.has_cli_process(sid, ('claude',))


def test_retirement_kills_the_session_without_orphans_and_keeps_scrollback(world):
    w = world
    sid, out = spawn(w, lines=3000)
    assert wait(lambda: w.parent.has_cli_process(sid, ('claude',)))
    assert wait(lambda: '3000' in w.parent.capture_output(sid))
    snapshot = w.run / 'handoffs' / 'ops-a-g0.terminal.txt'
    w.parent.capture_to(sid, str(snapshot))  # history page: the whole scrollback, not the current screen
    lines = snapshot.read_text().split()
    assert '1' in lines and '1500' in lines and '3000' in lines
    assert w.parent.kill_pane(sid) and w.parent.get_pane_status(sid) is None
    assert 'ops-a-g0' not in cm.tmux_sessions(w.sock)
    assert wait(lambda: not claude_pids(w))  # no orphan CLI left in the tmux server
    assert '1500' in out.read_text().split()
    assert not any(t.alive() for t in w.fake.tabs())
    assert cm.tmux_kill_server(w.sock)


def test_handoff_keeps_both_generations_until_the_old_one_retires(world):
    w = world
    old, _ = spawn(w, generation=0)
    new, _ = spawn(w, generation=1)  # the predecessor still has its tab: never reaped by the spawn
    assert set(cm.tmux_sessions(w.sock)) == {'ops-a-g0', 'ops-a-g1'}
    assert w.parent.tab_into(new, old)
    assert w.parent.kill_pane(old)
    assert cm.tmux_sessions(w.sock) == ['ops-a-g1'] and w.parent.has_cli_process(new, ('claude',))
    assert len(claude_pids(w)) == 1


def test_resume_without_the_tab_starts_the_next_generation_and_reaps_the_old_session(world):
    w = world
    old, out = spawn(w, generation=1, lines=50)
    assert wait(lambda: '50' in w.parent.capture_output(old).split())
    for tab in w.fake.tabs():
        tab.kill()  # the tab was closed (cmux restarted); the session is still in the tmux server
    assert wait(lambda: w.parent.get_pane_status(old) is None)
    assert cm.tmux_sessions(w.sock) == ['ops-a-g1']
    new, _ = spawn(w, generation=2)
    assert cm.tmux_sessions(w.sock) == ['ops-a-g2']
    assert wait(lambda: len(claude_pids(w)) == 1)
    assert '50' in out.read_text().split()  # the reaped generation's history was kept
    assert w.parent.kill_pane(old)  # forgetting the closed tab never touches the live generation
    assert cm.tmux_sessions(w.sock) == ['ops-a-g2']


def test_fleet_exit_ends_its_tmux_server_and_leaves_other_servers_alone(world):
    w = world
    other = str(w.run / 'other.sock')
    subprocess.run([TMUX, '-S', other, '-f', '/dev/null', 'new-session', '-d', '-s', 'user', 'sleep 600'], check=True)
    try:
        spawn(w)
        from cmux_fleet import subfleet
        subfleet.stop_tmux({'run': str(w.run)})
        assert cm.tmux_sessions(w.sock) == []
        assert wait(lambda: not claude_pids(w))
        assert subprocess.run([TMUX, '-S', other, 'has-session', '-t', '=user']).returncode == 0
    finally:
        subprocess.run([TMUX, '-S', other, 'kill-server'], capture_output=True)


def test_failed_tab_leaves_no_session(world, monkeypatch):
    w = world
    monkeypatch.setattr(w.parent, '_new_surface', lambda control=False, command=None: (None, False))
    out = w.run / 'x.log'
    res = w.parent.spawn_interactive_cli(window_name='fx-ops-a', cli_command=w.claude + ' 600', output_file=str(out),
                                         start_directory=str(w.run), tmux_socket=w.sock, tmux_session='ops-a-g0')
    assert not res.success and cm.tmux_sessions(w.sock) == []


def test_session_probe_proves_a_stop_only_from_tmux_itself(world, monkeypatch):
    """Liveness evidence (runtime_v2.tab_missing): False only when tmux shows the session gone."""
    w = world
    sid, _ = spawn(w)
    plain = w.parent.spawn_window('worker-2', 'sleep 600', metadata={'type': 'worker'}, start_directory=str(w.run))
    assert w.parent.tmux_session_alive(sid) is True and w.parent.tmux_session_alive(plain.pane_id) is None
    other, _ = spawn(w, slot='ops-b')
    tmux(w, 'kill-session', '-t', '=ops-a-g0')  # the session ended; the server still answers
    assert w.parent.tmux_session_alive(sid) is False and w.parent.tmux_session_alive(other) is True
    tmux(w, 'kill-server')  # no server at all; its socket file is left behind
    assert Path(w.sock).exists() and w.parent.tmux_session_alive(other) is False
    monkeypatch.setattr(cm, 'TMUX', str(w.run / 'no-such-tmux'))  # tmux unreadable: no evidence either way
    assert w.parent.tmux_session_alive(other) is None


def test_closed_client_tab_is_reattached_with_its_registry_entry(world):
    w = world
    sid, out = spawn(w)
    peer = w.parent.spawn_window('worker-2', 'sleep 600', metadata={'type': 'worker'}, start_directory=str(w.run))
    assert wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    assert w.parent.reattach_client(sid, peer.pane_id) is None  # its tab is still there
    next(t for s, t in w.fake.ws[w.parent._where(sid)].items() if s == sid).kill()  # the ✕ on the tab
    assert wait(lambda: w.parent.get_pane_status(sid) is None)
    assert wait(lambda: not clients(w, 'ops-a-g0')) and 'ops-a-g0' in cm.tmux_sessions(w.sock)
    new = w.parent.reattach_client(sid, peer.pane_id)
    assert new and w.parent.owns(new) and wait(lambda: len(clients(w, 'ops-a-g0')) == 1)
    entry = w.parent._meta()['surfaces'][new]
    assert entry['tmux_role'] == 'owner' and entry['output_file'] == str(out) and entry['label'] == 'fx-ops-a'
    assert sid not in w.parent._meta()['surfaces'] and w.parent.has_cli_process(new, ('claude',))
    assert w.parent.kill_pane(new) and 'ops-a-g0' not in cm.tmux_sessions(w.sock)  # still the owner
    assert w.parent.reattach_client(new, peer.pane_id) is None  # the session is gone: nothing to reattach
