"""start --resume --advisor writes `advisor: on` into the roster and must still save config.json, not the roster.

Incident 2026-10-23 (Studio studio-ops): the advisor loop in resume() rebound `path`, so the config was saved
over fleet-config/roster.yaml, the daemon was launched on it, and the next resume died on invalid YAML."""
import json
import sys
from types import SimpleNamespace

from cmux_fleet import cmux_manager, fleet

WORKERS = ['memx-ops', 'tgog-ops']


def test_resume_with_advisor_keeps_config_and_roster_apart(tmp_path, monkeypatch):
    run = tmp_path / 'run'
    (run / 'fleet-config').mkdir(parents=True)
    roster_path = run / 'fleet-config' / 'roster.yaml'
    roster_path.write_text('workers:\n' + ''.join('  - name: ' + w + '\n' for w in WORKERS))
    cfg = dict(run=str(run), python=sys.executable, workers=WORKERS, lifecycle='service', mode='service',
               supervisor_cli='codex', worker_cli='claude', transport_version=2, allow_development_v2=True,
               roster=str(roster_path))
    fleet.save(run / 'config.json', cfg)
    monkeypatch.setattr(fleet, 'daemon_running', lambda path: False)
    monkeypatch.setattr(cmux_manager, 'cmux_available', lambda: True)
    monkeypatch.setattr(fleet, 'connect', lambda cfg: object())
    monkeypatch.setattr(fleet, 'LATEST', tmp_path / 'latest.json')
    commands = []
    mgr = SimpleNamespace(spawn_window=lambda *a, **kw: commands.append(a) or SimpleNamespace(success=True))
    monkeypatch.setattr(fleet, 'manager_for', lambda cfg: mgr)

    assert fleet.resume(run, SimpleNamespace(max_attempts=None, roster=None, advisor=True))

    text = roster_path.read_text()
    assert 'advisor: on' in text and not text.lstrip().startswith('{')  # still a roster, now with the switch
    assert fleet.load(run / 'config.json')['workers'] == WORKERS  # the config went to config.json
    assert commands[0][1].split()[commands[0][1].split().index('daemon') + 1] == str(run / 'config.json')
    assert json.loads((tmp_path / 'latest.json').read_text())['config'] == str(run / 'config.json')
    # a second resume (the keeper's next try) must not trip on the roster
    assert fleet.resume(run, SimpleNamespace(max_attempts=None, roster=None, advisor=True))
