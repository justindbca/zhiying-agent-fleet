"""Worker roster YAML: which worker slots this fleet runs, and their optional role files.

    workers:
      - name: exit            # required; same rules as --worker-names
        role: roles/exit.md   # optional; relative to this file's directory
      - name: reentry
        window: 1000000       # optional handoff overrides for this worker (docs/design.md §22)
        handoff_ratio: 0.7
        profile: flashnext-coder  # optional agent card id: this slot's next generation runs that card
    handoff:                  # optional: per CLI / CLI:model (exact runtime model string)
      claude:opus: {window: 1000000}
      codex: {ratio: 0.6}
    advisor: off              # optional, default off: on = consult the advisor before every dispatch

The daemon re-reads the file while running (hot add / graceful remove; advisor on / off without
a restart, also via `fleet advisor on|off` or the supervisor's decision key). A file with any
problem is rejected as a whole, with one reason per problem; the running roster stays.
A changed `profile` swaps that slot to its next generation once it is idle (same slot name,
docs/design.md §25 night switch); the card itself is resolved and checked by the daemon.
"""
import os
import re
from pathlib import Path

NAME_RE = r'[a-z][a-z0-9-]{0,30}'  # same as fleet.WORKER_NAME_RE: no dots (dots mark generations)
PROFILE_RE = r'[a-z][a-z0-9-]{0,63}'  # same as agent_registry.AGENT_ID_RE


def parse(raw, base):
    """Return (workers, roles): ordered names and {name: absolute role path}. ValueError lists every problem."""
    workers, roles, _ = parse_entries(raw, base)
    return workers, roles


def parse_entries(raw, base):
    """Return (workers, roles, profiles); profiles is {name: agent card id} for rows that set one."""
    try:
        import yaml
    except ImportError as exc:
        raise ValueError('PyYAML is required for --roster (pip install pyyaml)') from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError('invalid YAML: ' + ' '.join(str(exc).split())) from exc
    if (not isinstance(data, dict) or 'workers' not in data
            or not set(data) <= {'workers', 'limits', 'handoff', 'advisor'} or not isinstance(data['workers'], list)):
        raise ValueError('the roster must be a mapping with only a "workers" list '
                         '(and optional "limits", "handoff", "advisor")')
    parse_limits(data.get('limits'))
    advisor_value(data)
    if not data['workers']:
        raise ValueError('the roster needs at least one worker')
    errors, workers, roles, profiles = handoff_errors(data), [], {}, {}
    for i, item in enumerate(data['workers']):
        where = 'workers[' + str(i) + ']'
        if not isinstance(item, dict) or not set(item) <= {'name', 'role', 'profile'} | set(WORKER_HANDOFF) or 'name' not in item:
            errors.append(where + ': must be a mapping with "name" and optional "role", "profile", "window", "handoff_ratio" only')
            continue
        name = item['name']
        if not isinstance(name, str) or not re.fullmatch(NAME_RE, name) or name == 'supervisor':
            errors.append(where + ': name must match ' + NAME_RE + ' and not be "supervisor" (no dots)')
            continue
        if name in workers:
            errors.append(where + ': duplicate worker name ' + name)
            continue
        workers.append(name)
        if item.get('profile') is not None:
            profile = item['profile']
            if not isinstance(profile, str) or not re.fullmatch(PROFILE_RE, profile):
                errors.append(where + ' (' + name + '): profile must be an agent card id matching ' + PROFILE_RE)
                continue
            profiles[name] = profile
        if item.get('role') is not None:
            role = item['role']
            path = (Path(base) / Path(role).expanduser()) if isinstance(role, str) and role.strip() else None
            if path is None or not path.is_file():
                errors.append(where + ' (' + name + '): role file not found: ' + str(role))
                continue
            roles[name] = str(path.resolve())
    if errors:
        raise ValueError('\n'.join(errors))
    return workers, roles, profiles


CLIS = ('claude', 'codex', 'pi')
WORKER_HANDOFF = {'window': 'window', 'handoff_ratio': 'ratio'}  # per-worker key -> resolved field


def _bad_window(v):
    return isinstance(v, bool) or not isinstance(v, int) or v < 1


def _bad_ratio(v):
    return isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v < 1


def handoff_errors(data):
    """Validate the handoff table and per-worker overrides (docs/design.md §22)."""
    errors, table = [], data.get('handoff')
    if table is not None and not isinstance(table, dict):
        return ['handoff must be a mapping of "<cli>" or "<cli>:<model>" to {window?, ratio?}']
    for k, v in (table or {}).items():
        where = 'handoff[' + str(k) + ']'
        if not isinstance(k, str) or k.split(':', 1)[0] not in CLIS or k.endswith(':'):
            errors.append(where + ': key must be one of ' + ', '.join(CLIS) + ' or "<cli>:<model>"')
        elif not isinstance(v, dict) or not v or not set(v) <= {'window', 'ratio'}:
            errors.append(where + ': must be a mapping with "window" and/or "ratio" only')
        else:
            errors += [where + ': window must be a positive integer'] * ('window' in v and _bad_window(v['window']))
            errors += [where + ': ratio must be a number between 0 and 1'] * ('ratio' in v and _bad_ratio(v['ratio']))
    for i, item in enumerate(data.get('workers') or []):
        if isinstance(item, dict):
            where = 'workers[' + str(i) + ']'
            errors += [where + ': window must be a positive integer'] * ('window' in item and _bad_window(item['window']))
            errors += [where + ': handoff_ratio must be a number between 0 and 1'] * (
                'handoff_ratio' in item and _bad_ratio(item['handoff_ratio']))
    return errors


def handoff(raw):
    """(table, per_worker) from an already validated roster: {key: {window?, ratio?}}, {name: {window?, ratio?}}."""
    import yaml
    data = yaml.safe_load(raw)
    workers = {}
    for item in data['workers']:
        fields = {WORKER_HANDOFF[k]: item[k] for k in WORKER_HANDOFF if k in item}
        if fields:
            workers[item['name']] = fields
    return dict(data.get('handoff') or {}), workers


LIMIT_KEYS = ('max_depth', 'max_children', 'max_agents_total', 'child_workers_max')


def parse_limits(value):
    """Optional sub-fleet limits (docs/design.md §19): positive integers, known keys only."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError('limits must be a mapping of ' + ', '.join(LIMIT_KEYS))
    bad = [k for k, v in value.items() if k not in LIMIT_KEYS or isinstance(v, bool) or not isinstance(v, int) or v < 1]
    if bad:
        raise ValueError('limits: unknown key or not a positive integer: ' + ', '.join(map(str, bad)))
    return dict(value)


def load_limits(path):
    import yaml
    data = yaml.safe_load(Path(path).expanduser().read_bytes())
    return parse_limits(data.get('limits') if isinstance(data, dict) else None)


def load(path):
    path = Path(path).expanduser().resolve()
    return parse(path.read_bytes(), path.parent)


def load_entries(path):
    path = Path(path).expanduser().resolve()
    return parse_entries(path.read_bytes(), path.parent)


ADVISOR_VALUES = {True: True, False: False, 'on': True, 'off': False}  # YAML 1.1 reads bare on/off as booleans


def advisor_value(data):
    """The roster's advisor switch (advisor plan §2.1): True = on. Missing = off."""
    value = data.get('advisor', False) if isinstance(data, dict) else False
    if not isinstance(value, (bool, str)) or value not in ADVISOR_VALUES:
        raise ValueError('advisor must be on or off')
    return ADVISOR_VALUES[value]


def advisor(raw):
    """advisor_value of an already validated roster file's bytes."""
    import yaml
    return advisor_value(yaml.safe_load(raw))


def with_advisor(text, on):
    """The roster text with its top-level advisor line set (other lines and comments untouched)."""
    line = 'advisor: ' + ('on' if on else 'off')
    if re.search(r'(?m)^advisor:.*$', text):
        return re.sub(r'(?m)^advisor:.*$', line, text, count=1)
    return text + ('' if not text or text.endswith('\n') else '\n') + line + '\n'


def write_advisor(path, on, workers=()):
    """Set advisor on/off in the roster file; a missing file is created from ``workers`` (a run started
    without --roster). The result must still be a valid roster; written atomically. Returns the path."""
    path = Path(path).expanduser()
    if path.exists():
        text = path.read_text()
    else:
        text = ('# Created by cmux-fleet for the advisor switch: the workers of this run.\n'
                '# Edits apply while the run is running, like any roster.\nworkers:\n'
                + ''.join('  - name: ' + w + '\n' for w in workers))
    text = with_advisor(text, on)
    parse(text.encode(), path.parent)  # never write a roster the daemon would reject
    tmp = path.with_name('.' + path.name + '.tmp')
    tmp.write_text(text)
    os.replace(tmp, path)
    return path
