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


# ---------------------------------------------------------------- backend-gated programs (design §6)
# The fleet runs on one terminal backend, so `cmux` is required only for backend=cmux and `wezterm`
# only for backend=wezterm. Items without `when:` stay mandatory for every backend.

def _two_backends(tmp_path, monkeypatch, *, cmux=True, wezterm=True):
    """A manifest with one gated item per backend; `cmux`/`wezterm` answer only when asked to."""
    (tmp_path / 'deps.yaml').write_text(
        'python: {min: "3.0"}\npython_packages: req.txt\n'
        'programs:\n'
        '  git: {cmd: [git, --version], min: "2.0"}\n'
        '  cmux: {cmd: [cmux, --version], min: "0.65.0", when: cmux}\n'
        '  wezterm: {cmd: [wezterm, --version], min: "20240203", when: wezterm}\n')
    (tmp_path / 'req.txt').write_text('')
    installed = {'git': (2, 30), 'cmux': (0, 65, 0) if cmux else None,
                 'wezterm': (20240203,) if wezterm else None}

    def fake_probe(cmd):
        return installed.get(cmd[0])

    monkeypatch.setattr(check_deps, 'probe', fake_probe)
    monkeypatch.delenv(check_deps.BACKEND_ENV, raising=False)


def test_explicit_backend_requires_only_its_own_program(tmp_path, monkeypatch):
    _two_backends(tmp_path, monkeypatch, cmux=False, wezterm=True)
    rows = {r[0]: r for r in check_deps.check(tmp_path, backend='wezterm')}
    assert rows['wezterm'][3] and rows['git'][3]
    assert rows['cmux'][3] and rows['cmux'][1].startswith('skip')
    assert all(r[3] for r in rows.values())  # cmux missing does not fail a wezterm run


def test_explicit_backend_fails_when_its_program_is_missing(tmp_path, monkeypatch):
    _two_backends(tmp_path, monkeypatch, cmux=True, wezterm=False)
    rows = {r[0]: r for r in check_deps.check(tmp_path, backend='wezterm')}
    assert not rows['wezterm'][3] and rows['wezterm'][1] == 'missing'
    assert rows['cmux'][3]  # the other backend's program is not asked for


def test_environment_selects_the_backend(tmp_path, monkeypatch):
    _two_backends(tmp_path, monkeypatch, cmux=True, wezterm=False)
    monkeypatch.setenv(check_deps.BACKEND_ENV, 'cmux')
    rows = {r[0]: r for r in check_deps.check(tmp_path)}
    assert rows['cmux'][3] and rows['wezterm'][1].startswith('skip')


def test_no_choice_follows_the_backend_that_is_installed(tmp_path, monkeypatch):
    _two_backends(tmp_path, monkeypatch, cmux=False, wezterm=True)
    rows = {r[0]: r for r in check_deps.check(tmp_path)}  # only wezterm answers here
    assert rows['wezterm'][3] and not rows['wezterm'][1].startswith('skip')
    assert rows['cmux'][1].startswith('skip')


def test_no_choice_and_nothing_installed_still_names_what_to_install(tmp_path, monkeypatch):
    _two_backends(tmp_path, monkeypatch, cmux=False, wezterm=False)
    rows = {r[0]: r for r in check_deps.check(tmp_path)}
    checked = [n for n, r in rows.items() if not r[1].startswith('skip')]
    assert any(n in ('cmux', 'wezterm') for n in checked), 'a gated group must be checked'
    assert not all(r[3] for r in rows.values())


def test_unknown_backend_is_refused(tmp_path, monkeypatch, capsys):
    _two_backends(tmp_path, monkeypatch)
    assert check_deps.main(['--backend', 'zellij']) == 2
    assert 'unknown backend' in capsys.readouterr().err


def test_main_takes_backend_and_exit_code(tmp_path, monkeypatch, capsys):
    _two_backends(tmp_path, monkeypatch, cmux=True, wezterm=False)
    monkeypatch.setattr(check_deps, 'ROOT', tmp_path)  # the default root was bound at def time
    monkeypatch.setattr(check_deps.check, '__defaults__', (tmp_path, None))
    assert check_deps.main(['--backend', 'cmux']) == 0
    assert check_deps.main(['--backend', 'wezterm']) == 1
    assert 'FAIL  wezterm' in capsys.readouterr().out


def test_the_real_manifest_gates_cmux_and_wezterm_on_a_backend():
    """The shipped deps.yaml: both terminal backends are gated, everything else stays mandatory."""
    import yaml
    programs = yaml.safe_load((ROOT / 'deps.yaml').read_text())['programs']
    assert programs['cmux']['when'] == 'cmux' and programs['wezterm']['when'] == 'wezterm'
    assert programs['wezterm']['min'] == '20240203'
    for name in ('redis-server', 'codex', 'claude', 'pi', 'node', 'tmux', 'git'):
        assert not programs[name].get('when'), name  # not backend specific: still mandatory
