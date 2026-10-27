"""WezTerm backend against a real private mux server (design §7.3): spawn / send / get / list / kill.

Skipped only when ``wezterm`` or ``wezterm-mux-server`` is not on PATH (same rule as the real-Redis
tests). Everything runs in a private mux server the test started itself:

* its own ``HOME`` (the probe showed the server's pid lock lives in ``$HOME/.local/share/wezterm/pid``,
  so a shared HOME collides with the operator's own WezTerm — probe-report §1),
* its own ``WEZTERM_UNIX_SOCKET`` in a short ``tmp_path`` and ``--no-auto-start`` on every call,
* the pid the backend recorded is the only one killed, in a ``finally``.

Nothing here touches the operator's WezTerm, ``~/.cmux-fleet`` or ``~/.local/state/cmux-fleet``.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cmux_fleet.backends import wezterm as wz  # noqa: E402

WEZTERM = shutil.which('wezterm')
MUX = shutil.which('wezterm-mux-server')

pytestmark = pytest.mark.skipif(
    not (WEZTERM and MUX),
    reason='real WezTerm mux server integration: needs wezterm and wezterm-mux-server on PATH')


@pytest.fixture
def short_root():
    """pytest's per-test tmp path plus ``/wezterm/<token>/mux.sock`` is over sockaddr_un on macOS, and the
    backend refuses a long socket (no /tmp fallback); the runtime dir therefore lives under mkdtemp."""
    return Path(tempfile.mkdtemp(prefix='wzit-')).resolve()


@pytest.fixture
def live(short_root):
    """A started private mux server in its own HOME; stopped by the pid the backend recorded."""
    os.environ.pop('WEZTERM_UNIX_SOCKET', None)  # never inherit a socket that leads to the user's server
    m = wz.WezTermSessionManager('wz-it', state_dir=short_root / 'rt')
    try:
        assert m.get_or_create_session() is True, 'private mux server did not come up: ' + m.last_error
        yield m
    finally:
        # Even when the assertion above fails, a server may have come up: adopt its recorded pid so the
        # teardown stops it instead of leaking it (`end_mux_server` only ever signals our own pid).
        if m.mux_pid is None and m.runtime_root:
            m.mux_pid = m._server_pid()
        m.end_mux_server()


def wait_for(pred, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_private_server_is_isolated_from_the_operators_wezterm(live):
    """Our HOME/socket are ours; the operator's own server (if any) is not referenced anywhere."""
    assert Path(live.mux_home) != Path.home()
    assert Path(live.mux_home).is_dir()
    assert Path(live.mux_socket).exists()
    out = subprocess.run([WEZTERM, 'cli', '--no-auto-start', 'list', '--format', 'json'],
                         capture_output=True, text=True, env=live.cli_env(), timeout=30)
    assert out.returncode == 0, out.stderr
    # A fresh server has exactly the bootstrap shell pane; the operator's panes are nowhere in it.
    rows = json.loads(out.stdout)
    assert isinstance(rows, list)
    for row in rows:
        assert row['workspace'] in ('default', live.mux_workspace)


def test_spawn_send_get_list_kill_roundtrip(live, tmp_path):
    """The full §4.2 path on a real server: spawn a window, type, read it back, list it, kill it."""
    res = live.spawn_window('supervisor', '/bin/sh -c "echo READY-$((6*7)); exec /bin/cat"',
                            start_directory=str(tmp_path))
    assert res.success, res.error
    pane = res.pane_id
    try:
        assert wait_for(lambda: 'READY-42' in live.capture_output(pane, 50)), 'capture_output saw no output'

        assert live.send_prompt_to_idle(pane, 'typed-line', bracketed=False) is True
        assert wait_for(lambda: 'typed-line' in live.capture_output(pane, 50))

        status = live.get_pane_status(pane)
        assert status is not None and status.alive is True
        assert status.metadata['tty_name'].startswith('/dev/'), 'a real tty: process queries need it'
        assert live.owns(pane) is True

        assert live.rename(pane, 'supervisor-1') is None or True  # rename returns None; the label is the point
        assert wait_for(lambda: 'supervisor-1' in [p['tab_title'] for p in live._list()]), 'tab title not set'

        assert live.focus(pane) is True
    finally:
        assert live.kill_pane(pane) is True
    assert not wait_for(lambda: live.owns(pane), timeout=1), 'a killed pane is no longer ours'


def test_spawned_pane_sees_the_operators_real_home_not_the_mux_home(live, tmp_path):
    """The fake HOME isolates the *server's* pid lock only; an agent pane must still see the operator's
    real HOME, or its credentials/profile (``~/.claude``, ``~/.codex``, ``~/.gitconfig`` ...) vanish.
    Regression test for the P0 the supervisor caught: ``cli_env`` used to hand every ``wezterm`` call
    ``HOME=$mux_home``, so spawn inherited it."""
    operator_home = str(Path.home().resolve())
    assert os.environ.get('HOME'), 'the test itself needs a real HOME to compare against'
    res = live.spawn_window('home-probe', '/bin/sh -c "echo HOME-IS=$HOME; exec /bin/sleep 60"',
                            start_directory=str(tmp_path))
    assert res.success, res.error
    pane = res.pane_id
    try:
        assert wait_for(lambda: 'HOME-IS=' in (live.capture_output(pane, 80) or '')), 'no HOME line in the pane'
        out = live.capture_output(pane, 80)
        line = next(l for l in out.splitlines() if 'HOME-IS=' in l)
        seen = line.split('HOME-IS=', 1)[1].strip()
        assert seen == operator_home, 'the pane must run as the operator: %r != %r' % (seen, operator_home)
        assert seen != str(Path(live.mux_home).resolve()), 'the mux HOME is only for the server pid lock'
    finally:
        live.kill_pane(pane)


def test_send_key_controls_reach_a_real_foreground_program(live, tmp_path):
    """Esc / Ctrl-C / arrows as keys (the probe's finding, now through the backend on a real mux server).

    The prog records the raw bytes it reads into a file; ``--no-paste`` must deliver them byte-exact.
    """
    log = tmp_path / 'keys.bin'
    script = tmp_path / 'rec.py'
    script.write_text(
        'import os,sys,tty,termios,time\n'
        'fd=sys.stdin.fileno()\n'
        'old=termios.tcgetattr(fd)\n'
        'tty.setraw(fd)\n'
        'out=open(%r,"wb",buffering=0)\n'
        'deadline=time.time()+20\n'
        'while time.time()<deadline:\n'
        '    b=os.read(fd,4096)\n'
        '    if not b: break\n'
        '    out.write(b)\n'
        '    if b"\\x03" in b: break\n'
        'termios.tcsetattr(fd,termios.TCSADRAIN,old)\n' % str(log))
    res = live.spawn_window('keyrec', '%s %s' % (sys.executable, script), start_directory=str(tmp_path))
    assert res.success, res.error
    pane = res.pane_id
    try:
        assert wait_for(lambda: log.exists())
        assert live.send_key(pane, 'Escape') is True
        assert live.send_key(pane, 'Up') is True
        assert live.send_key(pane, 'C-c') is True
        assert wait_for(lambda: log.exists() and b'\x03' in log.read_bytes())
        assert log.read_bytes() == b'\x1b\x1b[A\x03', 'Esc, Up and Ctrl-C must arrive as keystrokes'
    finally:
        live.kill_pane(pane)


def test_two_private_servers_coexist_on_one_machine(live, tmp_path):
    """§4.1 head-of-risk, through the code: a second run's server lives beside the first, both usable."""
    other = wz.WezTermSessionManager('wz-it2', state_dir=Path(live.runtime_root).parent.parent / 'rt2')
    try:
        assert other.get_or_create_session() is True
        assert other.mux_socket != live.mux_socket and other.mux_pid != live.mux_pid
        first = live.spawn_window('a', '/bin/sleep 60', start_directory=str(tmp_path))
        second = other.spawn_window('b', '/bin/sleep 60', start_directory=str(tmp_path))
        assert first.success and second.success
        # Each server numbers its panes from 0, so the same id can exist in both: what proves isolation
        # is that each manager only answers for panes in *its own* workspace (fleet-wz-it / fleet-wz-it2).
        assert live.mux_workspace != other.mux_workspace
        for m, pane, foreign in ((live, first.pane_id, other), (other, second.pane_id, live)):
            assert m.owns(pane) is True
            rows = {r['pane_id']: r for r in m._list()}
            assert rows[int(pane)]['workspace'] == m.mux_workspace
            assert m.mux_workspace not in [r['workspace'] for r in foreign._list()]
        live.kill_pane(first.pane_id)
        other.kill_pane(second.pane_id)
    finally:
        other.end_mux_server()


def test_arrange_on_a_real_server_keeps_one_window(live, tmp_path):
    sup = live.spawn_window('supervisor', '/bin/sleep 60', start_directory=str(tmp_path))
    w1 = live.spawn_window('worker-1', '/bin/sleep 60', start_directory=str(tmp_path))
    w2 = live.spawn_window('worker-2', '/bin/sleep 60', start_directory=str(tmp_path))
    ctl = live.spawn_window('control', '/bin/sleep 60', metadata={'type': 'daemon'},
                            start_directory=str(tmp_path))
    assert all(r.success for r in (sup, w1, w2, ctl))
    try:
        assert live.arrange(sup.pane_id, [w1.pane_id, w2.pane_id], 'file:///history.html') is None
        rows = {p['pane_id']: p for p in live._list()}
        assert rows[int(sup.pane_id)]['window_id'] == rows[int(w1.pane_id)]['window_id']
        assert rows[int(w1.pane_id)]['tab_id'] != rows[int(sup.pane_id)]['tab_id']
        assert rows[int(ctl.pane_id)]['tab_id'] == rows[int(sup.pane_id)]['tab_id']
        assert rows[int(ctl.pane_id)]['top_row'] > rows[int(sup.pane_id)]['top_row']
    finally:
        for r in (sup, w1, w2, ctl):
            live.kill_pane(r.pane_id)
