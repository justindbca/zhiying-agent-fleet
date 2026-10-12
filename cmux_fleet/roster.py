"""Worker roster YAML: which worker slots this fleet runs, and their optional role files.

    workers:
      - name: exit            # required; same rules as --worker-names
        role: roles/exit.md   # optional; relative to this file's directory
      - name: reentry

The daemon re-reads the file while running (hot add / graceful remove). A file with any
problem is rejected as a whole, with one reason per problem; the running roster stays.
"""
import re
from pathlib import Path

NAME_RE = r'[a-z][a-z0-9-]{0,30}'  # same as fleet.WORKER_NAME_RE: no dots (dots mark generations)


def parse(raw, base):
    """Return (workers, roles): ordered names and {name: absolute role path}. ValueError lists every problem."""
    try:
        import yaml
    except ImportError as exc:
        raise ValueError('PyYAML is required for --roster (pip install pyyaml)') from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError('invalid YAML: ' + ' '.join(str(exc).split())) from exc
    if not isinstance(data, dict) or set(data) != {'workers'} or not isinstance(data['workers'], list):
        raise ValueError('the roster must be a mapping with only a "workers" list')
    if not data['workers']:
        raise ValueError('the roster needs at least one worker')
    errors, workers, roles = [], [], {}
    for i, item in enumerate(data['workers']):
        where = 'workers[' + str(i) + ']'
        if not isinstance(item, dict) or not set(item) <= {'name', 'role'} or 'name' not in item:
            errors.append(where + ': must be a mapping with "name" and optional "role" only')
            continue
        name = item['name']
        if not isinstance(name, str) or not re.fullmatch(NAME_RE, name) or name == 'supervisor':
            errors.append(where + ': name must match ' + NAME_RE + ' and not be "supervisor" (no dots)')
            continue
        if name in workers:
            errors.append(where + ': duplicate worker name ' + name)
            continue
        workers.append(name)
        if item.get('role') is not None:
            role = item['role']
            path = (Path(base) / Path(role).expanduser()) if isinstance(role, str) and role.strip() else None
            if path is None or not path.is_file():
                errors.append(where + ' (' + name + '): role file not found: ' + str(role))
                continue
            roles[name] = str(path.resolve())
    if errors:
        raise ValueError('\n'.join(errors))
    return workers, roles


def load(path):
    path = Path(path).expanduser().resolve()
    return parse(path.read_bytes(), path.parent)
