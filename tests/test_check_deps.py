"""deps.yaml / requirements.txt checker: version parsing, pins and the failing exit."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('check_deps', ROOT / 'scripts/check_deps.py')
check_deps = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_deps)


def test_version_parsing_and_minimums():
    v = check_deps.version
    assert v('codex-cli 0.162.1') == (0, 162, 1) and v('tmux 3.6a') == (3, 6) and v('nothing') is None
    assert check_deps.at_least(v('cmux 0.65.0 (108)'), '0.65.0')
    assert not check_deps.at_least(v('cmux 0.64.25 (106)'), '0.65.0')
    assert not check_deps.at_least(None, '1.0')


def test_missing_program_and_wrong_pin_fail(tmp_path):
    (tmp_path / 'deps.yaml').write_text('python: {min: "3.0"}\npython_packages: req.txt\n'
                                        'programs:\n  nope: {cmd: [definitely-not-a-program-xyz], min: "1.0"}\n')
    (tmp_path / 'req.txt').write_text('PyYAML==0.0.1\n')
    rows = {r[0]: r for r in check_deps.check(tmp_path)}
    assert rows['python'][3] and not rows['PyYAML'][3] and rows['nope'][1] == 'missing' and not rows['nope'][3]


def test_manifest_names_every_program_the_code_calls():
    names = set(__import__('yaml').safe_load((ROOT / 'deps.yaml').read_text())['programs'])
    assert {'redis-server', 'codex', 'claude', 'pi', 'cmux', 'node', 'tmux', 'git'} <= names
