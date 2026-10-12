"""Night switch (docs/design.md §20): same worker slots, another agent card per slot.

`start --roster day.yaml --night-roster night.yaml` names both rosters; they must list the same
worker names (a retired name can never come back, so a swap that renamed slots could not be undone).
`cmux-fleet night on|off` only writes <run>/night.json atomically (the /sleep trigger file). The daemon
reads it at the top of each tick, reloads the roster it points to, and every slot whose `profile`
changed becomes its next generation once idle (in-flight turns finish first). When the last pending
slot is READY, the supervisor gets one breakpoint brief (resume's summary: what is verified / done /
still missing, queued work kept on its slot). The supervisor itself is never switched here.

The brake only receives a placeholder: fx:night {on, brake_card} for the budget code to read.
"""
import json
import os
import time
from pathlib import Path

STATE_FILE = 'night.json'
NIGHT = 'fx:night'  # on=1|0, brake_card=night|day, since: read by the brake (placeholder until it exists)


def state_path(run):
    return Path(run) / STATE_FILE


def read_state(run):
    """{'on': bool, ...}; a missing or unreadable file means day."""
    try:
        data = json.loads(state_path(run).read_text())
    except (OSError, ValueError):
        return {'on': False}
    return data if isinstance(data, dict) and isinstance(data.get('on'), bool) else {'on': False}


def write_state(run, on, source='cli'):
    """Atomic write: the daemon never sees half a file."""
    data = {'on': bool(on), 'brake_card': 'night' if on else 'day', 'at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
            'source': source}
    path = state_path(run)
    tmp = path.with_name(path.name + '.' + str(os.getpid()) + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    os.replace(tmp, path)
    return data


def roster_for(cfg):
    """The roster file the daemon should follow now (None: this run has no roster)."""
    if cfg.get('roster_night') and read_state(cfg['run'])['on']:
        return cfg['roster_night']
    return cfg.get('roster')


def same_slots(day, night):
    """Both rosters must name the same slots; ValueError otherwise."""
    if set(day) != set(night):
        raise ValueError('the night roster must list the same worker names as the day roster '
                         '(day: ' + ', '.join(day) + '; night: ' + ', '.join(night) + ')')


def resolve_launches(cfg, profiles):
    """{slot: launch} for each roster profile, or ValueError listing every problem.

    The CLI pair is checked through runtime_v2.validate_config on the slot's merged config: that is
    the combination-table entry point (docs: cli_pairs.yaml) and it never gets looser here."""
    from cmux_fleet import profile_launch, runtime_v2
    from cmux_fleet.agent_registry import AgentRegistry, AgentRegistryError
    if not profiles:
        return {}
    try:
        registry = AgentRegistry(cfg.get('agent_registry') or None)
    except AgentRegistryError as exc:
        raise ValueError('agent registry: ' + str(exc)) from exc
    errors, launches = [], {}
    for slot, profile in profiles.items():
        try:
            launch = profile_launch.resolve_launch(registry.get(profile), registry, cfg)
            runtime_v2.validate_config(dict(cfg, **launch['overrides']))
        except KeyError:
            errors.append(slot + ': unknown agent profile ' + profile)
            continue
        except ValueError as exc:  # ProfileRuntimeError is a ValueError
            errors.append(slot + ': profile ' + profile + ': ' + str(exc))
            continue
        launches[slot] = launch
    if errors:
        raise ValueError('\n'.join(errors))
    return launches


def check_rosters(cfg, day_path, night_path):
    """Validate a day/night roster pair before start or `night on|off`. Returns both parsed rosters."""
    from cmux_fleet import roster
    parsed = {}
    for which, path in (('day', day_path), ('night', night_path)):
        try:
            workers, roles, profiles = roster.load_entries(path)
            resolve_launches(cfg, profiles)
        except (OSError, ValueError) as exc:
            raise ValueError(which + ' roster ' + str(path) + ' rejected:\n' + str(exc)) from exc
        parsed[which] = (workers, roles, profiles)
    same_slots(parsed['day'][0], parsed['night'][0])
    return parsed
