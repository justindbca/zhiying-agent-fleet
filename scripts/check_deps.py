#!/usr/bin/env python3
"""Check deps.yaml on this machine: the running interpreter, its packages (requirements.txt pins) and
the external programs on PATH. Prints one line per item; exit 1 when anything is missing or too old.

Programs may carry `when: cmux|wezterm` (design §6): the fleet runs on one terminal backend, so a
gated program is only required for the backend the run will use. The backend is
`--backend`, else `CMUX_FLEET_BACKEND`, else the one that is actually installed here (cmux on macOS
first, wezterm otherwise); with none installed the first gated group is checked so the output still
names what to install. Gated items of other backends print as `skip` and never fail the check."""
import os
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BACKENDS = ('cmux', 'wezterm')
BACKEND_ENV = 'CMUX_FLEET_BACKEND'


def version(text):
    """First version-looking token as a tuple: 'codex-cli 0.162.1' -> (0, 162, 1), 'tmux 3.6a' -> (3, 6).
    A single number counts as a version too: WezTerm releases are date stamps ('20240203-110809-5046fc22'
    -> (20240203,)), and the minimum in deps.yaml may be given unquoted or as a bare number."""
    m = re.search(r'\d+(?:\.\d+)*', text or '')
    return tuple(int(x) for x in m.group(0).split('.')) if m else None


def at_least(have, need):
    need = version(str(need))
    return have is not None and need is not None and have[:len(need)] >= need


def probe(cmd):
    """(have, raw) for one `cmd` entry: the parsed version, or None when it is missing / too old."""
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return version(done.stdout + done.stderr) if done.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def pick_backend(spec, backend=None):
    """The backend whose gated programs are required here. Explicit choice wins, then the environment,
    then whatever is installed: a gated program that answers `cmd` is taken as available."""
    chosen = backend or os.environ.get(BACKEND_ENV) or ''
    chosen = chosen.strip().lower()
    if chosen:
        if chosen not in BACKENDS:
            raise ValueError('unknown backend ' + chosen + ' (expected one of: ' + ', '.join(BACKENDS) + ')')
        return chosen
    gated = [(name, item.get('when')) for name, item in spec['programs'].items() if item.get('when')]
    for name, when in gated:  # installed and answering: this is the backend the machine can run
        if probe(spec['programs'][name]['cmd']) is not None:
            return when
    return gated[0][1] if gated else None  # nothing installed: check a group so the output names it


def check(root=ROOT, backend=None):
    spec = yaml.safe_load((root / 'deps.yaml').read_text())
    backend = pick_backend(spec, backend)
    rows = []
    py = sys.version_info[:3]
    rows.append(('python', '.'.join(map(str, py)), spec['python']['min'], at_least(py, spec['python']['min'])))
    for line in (root / spec['python_packages']).read_text().splitlines():
        line = line.split('#')[0].strip()
        if not line:
            continue
        name, want = line.split('==')
        try:
            have = metadata.version(name)
        except metadata.PackageNotFoundError:
            have = None
        rows.append((name, have or 'missing', '==' + want, have == want))
    for name, item in spec['programs'].items():
        if item.get('when') and item['when'] != backend:
            rows.append((name, 'skip (backend=' + item['when'] + ')', '>=' + item['min'], True))
            continue
        have = probe(item['cmd'])
        shown = '.'.join(map(str, have)) if have else 'missing'
        rows.append((name, shown, '>=' + item['min'], at_least(have, item['min'])))
    return rows


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    backend = None
    if argv:
        if len(argv) == 2 and argv[0] == '--backend':
            backend = argv[1]
        else:
            print('usage: check_deps.py [--backend cmux|wezterm]', file=sys.stderr)
            return 2
    try:
        rows = check(backend=backend)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for name, have, need, ok in rows:
        print(('ok  ' if ok else 'FAIL') + '  ' + name + ' ' + have + ' (need ' + need + ')')
    return 0 if all(r[3] for r in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
