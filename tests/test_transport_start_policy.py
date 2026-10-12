"""New starts use Redis mailboxes; legacy configs remain readable, never hot-flipped."""
from types import SimpleNamespace

import pytest

from cmux_fleet import cmux_manager, fleet, runtime_v2


def no_provisioning(monkeypatch):
    def forbidden():
        pytest.fail('transport rejection must precede cmux/Redis provisioning')
    monkeypatch.setattr(cmux_manager, 'cmux_available', forbidden)


def test_new_cli_defaults_to_v2_without_implicitly_granting_development_permission(monkeypatch):
    seen = []
    monkeypatch.setattr(fleet, 'start', seen.append)
    fleet.main(['start'])
    assert seen[0].transport_version == 2
    assert seen[0].allow_development_v2 is False


def test_new_cli_rejects_v1_before_any_provisioning(monkeypatch):
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit) as exc:
        fleet.main(['start', '--transport-version', '1'])
    assert exc.value.code == 2


def test_programmatic_start_cannot_bypass_v1_rejection(monkeypatch):
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match='require Redis transport v2'):
        fleet.start(SimpleNamespace(transport_version=1))


def test_new_all_pi_start_still_requires_development_opt_in(monkeypatch):
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match='allow-development-v2'):
        fleet.main(['start', '--supervisor-cli', 'pi', '--worker-cli', 'pi'])


@pytest.mark.parametrize('supervisor,worker', [('codex', 'pi'), ('pi', 'codex'), ('pi', 'claude')])
def test_unsupported_cli_is_not_silently_replaced(monkeypatch, supervisor, worker):
    no_provisioning(monkeypatch)
    with pytest.raises(SystemExit, match='all-Pi'):
        fleet.main(['start', '--supervisor-cli', supervisor, '--worker-cli', worker,
                    '--allow-development-v2'])


def test_legacy_config_validation_still_preserves_v1():
    assert runtime_v2.validate_config({}) == 1
    assert runtime_v2.validate_config({'transport_version': 1, 'worker_cli': 'claude'}) == 1
