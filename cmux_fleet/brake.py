"""Brake: one budget card per task / service, split by quota pool, plus one wall table.

Budget card (task spec ``budget`` or service config ``budget``)::

    {"pool": "pool-a" | "pool-b",
     "max_usd": 40,        # pool-a only: equivalent USD spent since the card started
     "max_week_pct": 85,   # pool-b only: the pool's weekly quota % (absolute)
     "max_calls": 200,     # deliveries to any agent
     "max_rounds": 60,     # accepted supervisor decisions
     "max_hours": 6,       # soft line (pause); the task ``hours`` stays the hard budget_exhausted
     "max_busy": 2}        # concurrency cap for workers (tighter of this and --max-busy)

At a line the daemon pauses (stop delivering and rotating, keep everything resumable) and
notifies. Pool numbers come from ``quota.json`` (the quota treasurer, design v1 batch 1.7);
missing, unreadable or stale means ``unknown``: shown, never trips. Walls (config/walls.yaml):
account-level -> switch side or stop; per-model pool -> same CLI one tier down.

Pure functions; fleet.py keeps the counters in Redis and calls these.
"""
import json
import math
import re
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WALLS = HERE / 'config' / 'walls.yaml'
QUOTA_FILE = Path.home() / '.cmux-fleet' / 'quota.json'
QUOTA_STALE = 3600  # seconds: an older pool reading is unknown
POOL_LINE = {'pool-a': ('max_usd', 'cash_used_usd'), 'pool-b': ('max_week_pct', 'week_used_pct')}
INT_LIMITS = ('max_calls', 'max_rounds', 'max_busy')
NUM_LIMITS = ('max_usd', 'max_week_pct', 'max_hours')
# (limit, counter) pairs that pause the fleet; max_busy only caps concurrency.
LINES = (('max_calls', 'calls'), ('max_rounds', 'rounds'), ('max_hours', 'hours'),
         ('max_usd', 'usd'), ('max_week_pct', 'week_pct'))
TURN_WALL_MAX = 400  # a turn text longer than this is a report, not a wall message


def validate_budget(raw):
    """Returns a normalized copy, None for no card, or raises ValueError."""
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        raise ValueError('budget must be a non-empty JSON object')
    known = ('pool',) + INT_LIMITS + NUM_LIMITS
    unknown = sorted(set(raw) - set(known))
    if unknown:
        raise ValueError('budget may only have ' + ' / '.join(known) + ' (got ' + ', '.join(unknown) + ')')
    pool = raw.get('pool')
    if pool is not None and pool not in POOL_LINE:
        raise ValueError('budget pool must be one of ' + ', '.join(POOL_LINE))
    for k in INT_LIMITS:
        if k in raw and (type(raw[k]) is not int or raw[k] < 1):
            raise ValueError('budget ' + k + ' must be a positive integer')
    for k in NUM_LIMITS:
        v = raw.get(k)
        if k in raw and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0):
            raise ValueError('budget ' + k + ' must be a positive number')
    if raw.get('max_week_pct', 0) > 100:
        raise ValueError('budget max_week_pct must be at most 100')
    for owner, (limit, _) in POOL_LINE.items():
        if limit in raw and pool != owner:
            raise ValueError('budget ' + limit + ' applies only to pool ' + owner
                             + ' (pool-a: walls + equivalent USD; pool-b: walls + weekly quota %)')
    return dict(raw)


def parse_raise(items, budget):
    """``resume --raise max_calls=400``: returns the budget with those limits replaced (validated)."""
    out = dict(budget or {})
    for item in items or []:
        name, sep, value = str(item).partition('=')
        if not sep or name not in INT_LIMITS + NUM_LIMITS:
            raise ValueError('--raise takes LIMIT=VALUE with LIMIT one of ' + ', '.join(INT_LIMITS + NUM_LIMITS))
        try:
            out[name] = int(value) if name in INT_LIMITS else float(value)
        except ValueError:
            raise ValueError('--raise ' + name + ' needs a number') from None
    return validate_budget(out) if out else None


def read_quota(path=None):
    """The treasurer's quota.json, or {} when missing / unreadable (never raises)."""
    try:
        data = json.loads(Path(path or QUOTA_FILE).expanduser().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def pool_reading(quota, pool, now=None):
    """The number a pool's card watches (cash_used_usd / week_used_pct), or None = unknown."""
    if pool not in POOL_LINE:
        return None
    row = (quota.get('pools') or {}).get(pool) if isinstance(quota.get('pools'), dict) else None
    if not isinstance(row, dict):
        return None
    at = row.get('at', quota.get('at'))
    value = row.get(POOL_LINE[pool][1])
    if (isinstance(at, bool) or not isinstance(at, (int, float)) or (now or time.time()) - at > QUOTA_STALE
            or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
        return None
    return float(value)


def counters(state, quota, now=None):
    """Current values from the brake hash (calls / rounds / started / usd_base) and quota.json."""
    now = now or time.time()
    budget = json.loads(state.get('budget') or 'null') or {}
    pool = budget.get('pool')
    out = {'calls': int(state.get('calls') or 0), 'rounds': int(state.get('rounds') or 0),
           'hours': round((now - float(state.get('started') or now)) / 3600, 3), 'usd': None, 'week_pct': None}
    reading = pool_reading(quota, pool, now)
    if pool == 'pool-a' and reading is not None and state.get('usd_base') not in (None, ''):
        out['usd'] = round(reading - float(state['usd_base']), 2)
    elif pool == 'pool-b':
        out['week_pct'] = reading
    return out


def trips(budget, values):
    """Lines reached: [{'limit', 'value', 'cap'}]. Unknown values never trip."""
    return [{'limit': limit, 'value': values.get(counter), 'cap': budget[limit]}
            for limit, counter in LINES
            if (budget or {}).get(limit) is not None and values.get(counter) is not None
            and values[counter] >= budget[limit]]


def describe(found):
    return '; '.join(t['limit'] + ' ' + str(t['value']) + '/' + str(t['cap']) for t in found)


# ------------------------------------------------------------------ walls
_TABLE = {}


def wall_table(path=None):
    """walls.yaml compiled, cached by mtime."""
    path = Path(path or WALLS)
    stamp = path.stat().st_mtime_ns
    cached = _TABLE.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    import yaml
    raw = yaml.safe_load(path.read_text()) or {}
    table = {'account': [re.compile(p, re.I) for p in raw.get('account') or []],
             'model_pool': [re.compile(p, re.I) for p in raw.get('model_pool') or []],
             'same_cli_downgrade': dict(raw.get('same_cli_downgrade') or {}),
             'aliases': dict(raw.get('aliases') or {}), 'default': dict(raw.get('default') or {})}
    _TABLE[str(path)] = (stamp, table)
    return table


def classify_wall(text, table=None):
    """None, or {'scope': 'account' | 'model_pool', 'line': <matched line>}. Account wins."""
    table = table or wall_table()
    lines = [x.strip() for x in (text or '').splitlines() if x.strip()]
    for scope in ('account', 'model_pool'):
        for line in lines:
            if any(p.search(line) for p in table[scope]):
                return {'scope': scope, 'line': line[:300]}
    return None


def downgrade(cli, model, table=None):
    """Same CLI one tier down, or '' (no lower tier: fall back to switch side / stop)."""
    table = table or wall_table()
    name = table['aliases'].get(model, model) if model else table['default'].get(cli, '')
    return table['same_cli_downgrade'].get(name, '')


def wall_action(wall, cli, model, table=None):
    """What the brake decides for a wall: downgrade (same CLI) or stop (account-level / no lower tier).

    Inside one fleet "switch side" is not available (the CLI pairs table decides that), so an
    account-level wall stops: the fleet pauses resumable and a human or the orchestrator acts."""
    if wall['scope'] == 'model_pool':
        to = downgrade(cli, model, table)
        if to:
            return {'action': 'downgrade', 'cli': cli, 'from': model, 'to': to}
    return {'action': 'stop', 'cli': cli, 'from': model,
            'advice': 'account-level wall: switch side (other CLI) or wait for the reset'
            if wall['scope'] == 'account' else 'no lower tier on this CLI: switch side or wait'}
