"""Worker roster YAML: which worker slots this fleet runs, and their optional role files.

    workers:
      - name: exit            # required; same rules as --worker-names
        role: roles/exit.md   # optional; relative to this file's directory
      - name: reentry
        profile: flashnext-coder  # optional agent card id: this slot's next generation runs that card

The daemon re-reads the file while running (hot add / graceful remove). A file with any
problem is rejected as a whole, with one reason per problem; the running roster stays.
A changed `profile` swaps that slot to its next generation once it is idle (same slot name,
docs/design.md §20 night switch); the card itself is resolved and checked by the daemon.
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
    if (not isinstance(data, dict) or 'workers' not in data or not set(data) <= {'workers', 'limits'}
            or not isinstance(data['workers'], list)):
        raise ValueError('the roster must be a mapping with only a "workers" list (and optional "limits")')
    parse_limits(data.get('limits'))
    if not data['workers']:
        raise ValueError('the roster needs at least one worker')
    errors, workers, roles, profiles = [], [], {}, {}
    for i, item in enumerate(data['workers']):
        where = 'workers[' + str(i) + ']'
        if not isinstance(item, dict) or not set(item) <= {'name', 'role', 'profile'} or 'name' not in item:
            errors.append(where + ': must be a mapping with "name" and optional "role" / "profile" only')
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
