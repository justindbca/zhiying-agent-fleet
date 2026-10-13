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
    subfleets:                # optional named sub-fleets (docs/design.md §28); worker ``exit`` supervises one
      - {name: ops, supervisor_slot: exit, supervisor_role: roles/ops-sup.md, roster: ops.yaml, autostart: false}

The daemon re-reads the file while running (hot add / graceful remove). A file with any
problem is rejected as a whole, with one reason per problem; the running roster stays.
A changed `profile` swaps that slot to its next generation once it is idle (same slot name,
docs/design.md §25 night switch); the card itself is resolved and checked by the daemon.
"""
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
            or not set(data) <= {'workers', 'limits', 'handoff', 'subfleets'} or not isinstance(data['workers'], list)):
        raise ValueError('the roster must be a mapping with only a "workers" list '
                         '(and optional "limits", "handoff", "subfleets")')
    parse_limits(data.get('limits'))
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
    errors += subfleet_errors(data.get('subfleets'), base, workers)
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


# ------------------------------------------------------------------ named sub-fleets (docs/design.md §28)
SUBFLEET_KEYS = {'name', 'supervisor_slot', 'supervisor_role', 'autostart', 'mode', 'roster', 'claude',
                 'worker_model', 'timeouts', 'max_busy', 'drain_s'}
SUBFLEET_MODES = ('service', 'task')
DRAIN_S = 600
TIMEOUT_KEYS = {'stall': 'stall_timeout', 'request': 'request_timeout'}  # entry timeouts -> fleet config key


def _positive(v):
    return not isinstance(v, bool) and isinstance(v, (int, float)) and v > 0


def subfleet_entries(value, base, workers):
    """(entries, errors) of the optional ``subfleets:`` list. Each entry is a dict with every key resolved:
    paths absolute, ``timeouts`` split into stall_timeout / request_timeout, drain_s defaulted."""
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ['subfleets must be a list of mappings']
    entries, errors, names, slots = [], [], set(), set()
    for i, item in enumerate(value):
        where = 'subfleets[' + str(i) + ']'
        if not isinstance(item, dict) or not set(item) <= SUBFLEET_KEYS:
            errors.append(where + ': must be a mapping with keys ' + ', '.join(sorted(SUBFLEET_KEYS)) + ' only')
            continue
        name = item.get('name')
        if not isinstance(name, str) or not re.fullmatch(NAME_RE, name) or name == 'supervisor':
            errors.append(where + ': name must match ' + NAME_RE + ' and not be "supervisor"')
            continue
        where += ' (' + name + ')'
        bad = []
        if name in names:
            bad.append('duplicate sub-fleet name')
        if name in workers:
            bad.append('name is also a worker name')
        slot = item.get('supervisor_slot')
        if slot not in workers:
            bad.append('supervisor_slot must be one of the workers: ' + ', '.join(workers))
        elif slot in slots:
            bad.append('supervisor_slot ' + slot + ' already supervises another sub-fleet')
        out = {'name': name, 'supervisor_slot': slot, 'autostart': item.get('autostart', False),
               'mode': item.get('mode', 'service'), 'drain_s': item.get('drain_s', DRAIN_S)}
        for key in ('supervisor_role', 'roster'):
            path = item.get(key)
            path = (Path(base) / Path(path).expanduser()) if isinstance(path, str) and path.strip() else None
            if path is None or not path.is_file():
                bad.append(key + ' file not found: ' + str(item.get(key)))
            else:
                out[key] = str(path.resolve())
        if type(out['autostart']) is not bool:
            bad.append('autostart must be true or false')
        if out['mode'] not in SUBFLEET_MODES:
            bad.append('mode must be one of ' + ', '.join(SUBFLEET_MODES))
        for key in ('claude', 'worker_model'):
            if key in item:
                if not isinstance(item[key], str) or not item[key].strip():
                    bad.append(key + ' must be a non-empty string')
                else:
                    out[key] = str(Path(base) / Path(item[key]).expanduser()) if (
                        key == 'claude' and '/' in item[key]) else item[key]
        timeouts = item.get('timeouts')
        if timeouts is not None:
            if not isinstance(timeouts, dict) or not set(timeouts) <= set(TIMEOUT_KEYS) \
                    or not all(_positive(v) for v in timeouts.values()):
                bad.append('timeouts must be a mapping of stall / request to positive seconds')
            else:
                out.update({TIMEOUT_KEYS[k]: v for k, v in timeouts.items()})
        if 'max_busy' in item:
            if isinstance(item['max_busy'], bool) or not isinstance(item['max_busy'], int) or item['max_busy'] < 0:
                bad.append('max_busy must be a non-negative integer')
            else:
                out['max_busy'] = item['max_busy']
        if not _positive(out['drain_s']):
            bad.append('drain_s must be a positive number of seconds')
        if 'roster' in out and not bad:
            try:
                load_entries(out['roster'])
            except (OSError, ValueError) as exc:
                bad.append('sub-fleet roster ' + out['roster'] + ' rejected: ' + ' '.join(str(exc).split()))
        names.add(name)
        if slot in workers:
            slots.add(slot)
        if bad:
            errors += [where + ': ' + b for b in bad]
        else:
            entries.append(out)
    return entries, errors


def subfleet_errors(value, base, workers):
    return subfleet_entries(value, base, workers)[1]


def subfleets(raw, base):
    """Validated ``subfleets:`` entries of an already validated roster (empty list without the section)."""
    import yaml
    data = yaml.safe_load(raw)
    workers = [w['name'] for w in data['workers']]
    return subfleet_entries(data.get('subfleets'), base, workers)[0]


def load_subfleets(path):
    path = Path(path).expanduser().resolve()
    return subfleets(path.read_bytes(), path.parent)
