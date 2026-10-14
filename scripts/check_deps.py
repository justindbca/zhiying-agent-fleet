#!/usr/bin/env python3
"""Check deps.yaml on this machine: the running interpreter, its packages (requirements.txt pins) and
the external programs on PATH. Prints one line per item; exit 1 when anything is missing or too old."""
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def version(text):
    m = re.search(r'\d+(?:\.\d+)+', text or '')
    return tuple(int(x) for x in m.group(0).split('.')) if m else None


def at_least(have, need):
    need = version(need)
    return have is not None and have[:len(need)] >= need


def check(root=ROOT):
    spec = yaml.safe_load((root / 'deps.yaml').read_text())
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
        try:
            done = subprocess.run(item['cmd'], capture_output=True, text=True, timeout=20)
            have = version(done.stdout + done.stderr) if done.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            have = None
        shown = '.'.join(map(str, have)) if have else 'missing'
        rows.append((name, shown, '>=' + item['min'], at_least(have, item['min'])))
    return rows


def main():
    rows = check()
    for name, have, need, ok in rows:
        print(('ok  ' if ok else 'FAIL') + '  ' + name + ' ' + have + ' (need ' + need + ')')
    return 0 if all(r[3] for r in rows) else 1


if __name__ == '__main__':
    sys.exit(main())
