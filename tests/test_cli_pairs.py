"""Transport v2 CLI pair table: config/cli_pairs.yaml decides who may run."""
from pathlib import Path

import pytest
import yaml

from cmux_fleet import cli_pairs, runtime_v2, transport_inventory

ROOT = Path(__file__).resolve().parents[1]


def write(tmp_path, pairs, default=None):
    path = tmp_path / 'cli_pairs.yaml'
    path.write_text(yaml.safe_dump({'version': 1, 'default': default or {}, 'pairs': pairs}))
    return path


def row(sup, wrk, mode, status='verified', missing=(), **extra):
    return dict(supervisor_cli=sup, worker_cli=wrk, mode=mode, status=status,
                adapter_missing=list(missing), verified_by=None, **extra)


def test_shipped_table_rows_and_default():
    table = cli_pairs.load_pairs()
    assert table.default == {'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'task'}
    assert table.lookup('codex', 'claude', 'task')['status'] == 'verified'
    for mode in ('task', 'service'):
        assert table.lookup('pi', 'pi', mode)['status'] == 'verified'
        assert table.lookup('codex', 'pi', mode)['status'] == 'unverified'
        assert table.lookup('codex', 'pi', mode)['adapter_missing'] == []
        # Claude supervisor adapter landed (A3): unverified, smoke-only, nothing missing.
        assert table.lookup('claude', 'pi', mode)['adapter_missing'] == []
        assert table.lookup('claude', 'pi', mode)['status'] == 'unverified'
    assert table.lookup('codex', 'claude', 'service')['adapter_missing'] == ['codex_service_renderer']
    assert table.lookup('claude', 'claude', 'task')['adapter_missing'] == []
    assert table.lookup('claude', 'claude', 'task')['status'] == 'unverified'
    assert table.lookup('claude', 'claude', 'service') is None and table.lookup('claude', 'codex', 'task') is None
    assert table.lookup('codex', 'codex', 'task')['status'] == 'unverified'
    assert table.lookup('pi', 'codex', 'task') is None


def test_verified_runs_and_default_pair_runs():
    assert cli_pairs.pair_problem({'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'task'}) is None
    assert cli_pairs.pair_problem({'supervisor_cli': 'pi', 'worker_cli': 'pi', 'mode': 'service'}) is None


def test_unverified_runs_only_under_smoke_lifecycle():
    cfg = {'supervisor_cli': 'codex', 'worker_cli': 'pi', 'mode': 'task'}
    assert '--lifecycle smoke' in cli_pairs.pair_problem(cfg)
    assert cli_pairs.pair_problem(dict(cfg, lifecycle='dev_task'))
    assert cli_pairs.pair_problem(dict(cfg, lifecycle='smoke')) is None
    assert cli_pairs.pair_problem({'supervisor_cli': 'codex', 'worker_cli': 'codex', 'lifecycle': 'smoke'}) is None


def test_adapter_missing_refused_even_for_smoke(tmp_path):
    reason = cli_pairs.pair_problem({'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'service',
                                     'lifecycle': 'smoke'})
    assert reason and 'adapter missing' in reason
    table = cli_pairs.load_pairs(write(tmp_path, [row('claude', 'pi', 'task', 'unverified', ['claude_supervisor'])]))
    reason = cli_pairs.pair_problem({'supervisor_cli': 'claude', 'worker_cli': 'pi', 'lifecycle': 'smoke'}, table)
    assert reason and 'adapter missing: claude_supervisor' in reason


def test_forbidden_and_unlisted_refused_even_for_smoke(tmp_path):
    table = cli_pairs.load_pairs(write(tmp_path, [row('pi', 'pi', 'task'),
                                                  row('codex', 'pi', 'task', 'forbidden', reason='night only')]))
    assert cli_pairs.pair_problem({'supervisor_cli': 'codex', 'worker_cli': 'pi', 'lifecycle': 'smoke'},
                                  table) == 'night only'
    assert 'mixed Pi/Codex' in cli_pairs.pair_problem(
        {'supervisor_cli': 'pi', 'worker_cli': 'codex', 'lifecycle': 'smoke'}, table)
    assert cli_pairs.pair_problem({'supervisor_cli': 'pi', 'worker_cli': 'pi', 'mode': 'service'}, table)
    assert cli_pairs.pair_problem({'supervisor_cli': 'pi', 'worker_cli': 'pi'}, table) is None


def test_adopted_claude_subfleet_exception_is_task_only():
    base = {'supervisor_cli': 'claude', 'worker_cli': 'claude', 'mode': 'task'}
    assert '--lifecycle smoke' in cli_pairs.pair_problem(base)
    assert cli_pairs.pair_problem(dict(base, lifecycle='smoke')) is None
    assert cli_pairs.pair_problem(dict(base, adopt={'pane': 'x'})) is None
    assert cli_pairs.pair_problem(dict(base, adopt={'pane': 'x'}, mode='service'))


def test_table_is_reread_on_every_check(tmp_path, monkeypatch):
    path = write(tmp_path, [row('codex', 'pi', 'task', 'unverified')])
    monkeypatch.setattr(cli_pairs, 'DEFAULT_PATH', path)
    cfg = dict(transport_version=2, allow_development_v2=True, supervisor_cli='codex', worker_cli='pi')
    with pytest.raises(ValueError, match='lifecycle smoke'):
        runtime_v2.validate_config(cfg)
    path.write_text(yaml.safe_dump({'version': 1, 'pairs': [row('codex', 'pi', 'task')]}))
    assert runtime_v2.validate_config(cfg) == 2
    with pytest.raises(ValueError, match='allow-development-v2'):
        runtime_v2.validate_config(dict(cfg, allow_development_v2=False))


def test_smoke_does_not_relax_development_opt_in():
    cfg = dict(transport_version=2, supervisor_cli='codex', worker_cli='pi', lifecycle='smoke')
    with pytest.raises(ValueError, match='allow-development-v2'):
        runtime_v2.validate_config(cfg)


@pytest.mark.parametrize('data,match', [
    ({'pairs': []}, 'version'),
    ({'version': 1, 'pairs': [row('gemini', 'pi', 'task')]}, 'bad cli pair'),
    ({'version': 1, 'pairs': [row('pi', 'pi', 'task', 'maybe')]}, 'status'),
    ({'version': 1, 'pairs': [row('pi', 'pi', 'task'), row('pi', 'pi', 'task')]}, 'duplicate'),
    ({'version': 1, 'default': {'supervisor_cli': 'codex', 'worker_cli': 'claude', 'mode': 'task'},
      'pairs': [row('pi', 'pi', 'task')]}, 'default pair')])
def test_malformed_table_rejected(tmp_path, data, match):
    path = tmp_path / 'bad.yaml'
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match=match):
        cli_pairs.load_pairs(path)


def test_inventory_uses_same_rule_and_wording():
    assert transport_inventory.v2_cli_problem is runtime_v2.v2_cli_problem
    cfg = {'supervisor_cli': 'codex', 'worker_cli': 'pi', 'mode': 'task'}
    assert transport_inventory.v2_cli_problem(cfg) == cli_pairs.pair_problem(cfg)


def test_yaml_ships_as_package_data():
    tomllib = pytest.importorskip('tomllib')
    data = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    globs = data['tool']['setuptools']['package-data']['cmux_fleet']
    shipped = {p for g in globs for p in (ROOT / 'cmux_fleet').glob(g)}
    assert cli_pairs.DEFAULT_PATH in shipped
