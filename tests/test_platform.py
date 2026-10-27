"""Design §6: the platform seam. `keep_awake_prefix()` replaces the three hardcoded `caffeinate -i`
sites, so the daemon keeps the machine awake on macOS (caffeinate) and Linux (systemd-inhibit), and
runs unaffected on a host with neither. Everything here is pure: `platform` and `which` are injected,
so the expectations hold on any host, macOS or Linux."""
import re
from pathlib import Path

from cmux_fleet import platform as plat

ROOT = Path(__file__).resolve().parents[1]


def test_macos_uses_caffeinate_idle_only():
    """Old behaviour, unchanged: `caffeinate -i` (idle sleep only, not `-d` display)."""
    assert plat.keep_awake_prefix('darwin', lambda name: '/usr/bin/' + name) == ['/usr/bin/caffeinate', '-i']


def test_linux_uses_systemd_inhibit_when_present():
    prefix = plat.keep_awake_prefix('linux', lambda name: '/usr/bin/' + name)
    assert prefix[:2] == ['/usr/bin/systemd-inhibit', '--what=idle:sleep']
    assert prefix[2].startswith('--why=')


def test_linux_without_systemd_inhibit_adds_no_prefix():
    """No tool: the daemon still runs; the machine may sleep. Never raise, never guess a substitute."""
    assert plat.keep_awake_prefix('linux', lambda name: None) == []


def test_macos_without_caffeinate_adds_no_prefix():
    assert plat.keep_awake_prefix('darwin', lambda name: None) == []


def test_unknown_platform_adds_no_prefix():
    """Windows / anything else is out of scope (design §8): no prefix rather than a wrong one."""
    assert plat.keep_awake_prefix('win32', lambda name: '/usr/bin/' + name) == []
    assert plat.keep_awake_prefix('freebsd13', lambda name: '/usr/bin/' + name) == []


def test_linux_does_not_look_up_caffeinate_and_macos_not_systemd_inhibit():
    asked = []
    plat.keep_awake_prefix('linux', lambda name: asked.append(name) or '/usr/bin/' + name)
    plat.keep_awake_prefix('darwin', lambda name: asked.append(name) or '/usr/bin/' + name)
    assert asked == ['systemd-inhibit', 'caffeinate']


def test_default_platform_is_the_running_one():
    import sys
    prefix = plat.keep_awake_prefix()
    if sys.platform == 'darwin':
        assert prefix == ['/usr/bin/caffeinate', '-i'] or prefix == []
    else:
        assert prefix == [] or prefix[1:] == ['--what=idle:sleep', '--why=cmux-fleet']


def test_the_three_caffeinate_sites_call_the_seam():
    """No hardcoded `caffeinate -i` construction is left in the daemon start paths (design §6)."""
    for name in ('fleet.py', 'subfleet.py'):
        text = (ROOT / 'cmux_fleet' / name).read_text()
        assert "shutil.which('caffeinate')" not in text, name
        assert "'-i'] + command" not in text, name
        assert 'keep_awake_prefix() + command' in text, name
    # two daemon spawns in fleet.py (start + --resume), one in subfleet.default_launch
    assert (ROOT / 'cmux_fleet' / 'fleet.py').read_text().count('keep_awake_prefix() + command') == 2
    assert (ROOT / 'cmux_fleet' / 'subfleet.py').read_text().count('keep_awake_prefix() + command') == 1


def test_start_prepends_the_prefix_to_the_daemon_command(tmp_path, monkeypatch):
    """End to end through `fleet.main`: the daemon command really carries the prefix of the platform.

    test_pi.mock_start builds a whole `start` invocation with a fake manager; only spawn_window is
    replaced here so the command string the daemon is launched with can be read."""
    from types import SimpleNamespace
    from cmux_fleet import cmux_manager, fleet
    import test_pi

    seen = []

    def fake_prefix():
        seen.append(True)
        return ['/fake/caffeinate', '-i']

    monkeypatch.setattr(fleet, 'keep_awake_prefix', fake_prefix)
    args, _ = test_pi.mock_start(tmp_path, monkeypatch)
    spawned = []
    spy = SimpleNamespace(meta_path=tmp_path / 'isolated-state' / 'cmux-fleet.json',
                          session_exists=lambda: False,
                          claim_run=lambda run: True,
                          spawn_window=lambda name, command, **kw: (spawned.append((name, command)),
                                                                    SimpleNamespace(success=True))[1])
    monkeypatch.setattr(cmux_manager, 'CmuxSessionManager', lambda name: spy)
    # layout / browser / tmux calls after the daemon spawn are irrelevant: make them no-ops that succeed
    monkeypatch.setattr(fleet, 'arrange', lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: test_pi.FakeRedis())

    fleet.main(args)
    daemon = [command for name, command in spawned if name == 'control']
    assert daemon, spawned
    assert daemon[0].startswith('/fake/caffeinate -i ')
    assert seen, 'keep_awake_prefix was not called on the start path'
