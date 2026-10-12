"""Which model every worker slot runs now, and the card limits checked before a slot takes a card.

Counting rules (docs/design.md §12 模型特点卡; interface for the night switch and the brake):
- The unit is the *model*: a profile card counts under its ``model_card`` (claude-coder and
  claude-reviewer both count as ``opus``); a card without ``model_card`` counts under its own id.
- One slot counts once, whatever its generation: a handoff successor or a profile switch inside
  the same slot never doubles the count (same-slot reuse is free).
- A slot runs the card frozen in its ``runtime_launch`` (set before the successor is spawned, so a
  switch decided earlier in the same tick is already counted); without one it runs the fleet
  default, mapped to a model card only by an exact (cli, model string) match. No match = unmapped,
  never guessed, never counted.
- Dead slots hold nothing. Retiring slots still hold their model until they are gone.
- The supervisor is not counted (its Codex profile name does not say which model card it is).
"""
import json
from typing import Dict, Mapping, Optional, Tuple

from cmux_fleet.agent_registry import AgentCard, AgentRegistry


def limit_key(card) -> str:
    """The id max_concurrent is counted under."""
    return getattr(card, 'model_card', None) or card.id


def default_model(cfg: Mapping, registry: AgentRegistry) -> Optional[str]:
    """Model card id of the fleet's default worker runtime, or None (unknown: not counted)."""
    cli = cfg.get('worker_cli') or 'codex'
    model = {'pi': cfg.get('pi_model'), 'claude': cfg.get('worker_model'),
             'codex': cfg.get('codex_model')}.get(cli)
    if not model:
        return None
    matches = [m.id for m in registry.models if m.cli == cli and m.model == model]
    return matches[0] if len(matches) == 1 else None


def slot_card(info: Mapping, cfg: Mapping, registry: AgentRegistry) -> Tuple[Optional[str], Optional[str]]:
    """(profile id or None, model key or None) for one worker slot's current generation."""
    try:
        launch = json.loads(info.get('runtime_launch') or '{}')
    except ValueError:
        launch = {}
    profile = launch.get('profile_id') or None
    if not profile:
        return None, default_model(cfg, registry)
    try:
        return profile, limit_key(registry.get(profile))
    except KeyError:
        return profile, None  # a card removed after spawn: unknown, not guessed


class Usage:
    """Snapshot {slot: (profile, model key)} of the live worker slots."""

    def __init__(self, slots: Mapping[str, Tuple[Optional[str], Optional[str]]]):
        self.slots = dict(slots)

    @classmethod
    def from_agents(cls, agents: Mapping[str, Mapping], cfg: Mapping, registry: AgentRegistry) -> 'Usage':
        return cls({slot: slot_card(info, cfg, registry) for slot, info in agents.items()
                    if info.get('state') != 'dead'})

    def holders(self, key: str, exclude: Optional[str] = None):
        return sorted(slot for slot, (_, model) in self.slots.items() if model == key and slot != exclude)

    def counts(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for _, model in self.slots.values():
            if model:
                out[model] = out.get(model, 0) + 1
        return out

    def for_planning(self, reusable) -> Dict[str, int]:
        """Router occupancy for a plan that may reuse ``reusable`` slots: a model already running on
        one of them can take the work there without a new slot, so that slot is not counted."""
        reusable = set(reusable)
        out = self.counts()
        for key in list(out):
            if any(self.slots[s][1] == key for s in reusable if s in self.slots):
                out[key] -= 1
        return out

    def admit(self, slot: str, card: AgentCard) -> Optional[str]:
        """None if ``slot`` may run ``card`` now; otherwise why not (wait for a slot to free up)."""
        if card.max_concurrent is None:
            return None
        key = limit_key(card)
        others = self.holders(key, exclude=slot)
        if len(others) + 1 > card.max_concurrent:
            return ('max_concurrent %d for model %s reached (running on %s)'
                    % (card.max_concurrent, key, ', '.join(others)))
        return None


def over_cap(usage: Usage, registry: AgentRegistry):
    """Models running on more slots than their model card's max_concurrent (fleet default included)."""
    out = []
    for key, n in sorted(usage.counts().items()):
        try:
            cap = registry.model(key).max_concurrent
        except KeyError:
            continue
        if cap is not None and n > cap:
            out.append('model %s runs on %d slots (%s), max_concurrent %d'
                       % (key, n, ', '.join(usage.holders(key)), cap))
    return out


def host_problem(card: AgentCard, host: Optional[str]) -> Optional[str]:
    """None if the card may run on ``host`` (unset host or card without hosts: no limit)."""
    from cmux_fleet.router import host_matches
    if host_matches(card, host):
        return None
    return 'profile %s runs only on %s, this fleet is on %s' % (card.id, ', '.join(card.hosts), host)


def check_slots(cfg: Mapping, workers, profiles: Mapping[str, str], registry: AgentRegistry):
    """Errors for a whole roster: ``profiles`` {slot: card id}, other slots run the fleet default.

    Each slot holds one model; per model, slots with a capped card must fit its max_concurrent.
    Default (profile-less) slots are counted when their model maps, but only the card-carrying slots
    are refused (the fleet default was accepted at start). Host limits apply to every card."""
    errors, slots, caps = [], {}, {}
    host = cfg.get('host') or None
    default = default_model(cfg, registry)
    for slot in workers:
        profile = profiles.get(slot)
        if not profile:
            slots[slot] = (None, default)
            continue
        try:
            card = registry.get(profile)
        except KeyError:
            errors.append(slot + ': unknown agent profile ' + profile)
            continue
        problem = host_problem(card, host)
        if problem:
            errors.append(slot + ': ' + problem)
        slots[slot] = (profile, limit_key(card))
        if card.max_concurrent is not None:
            caps.setdefault(limit_key(card), []).append((slot, card.max_concurrent))
    usage = Usage(slots)
    for key, entries in caps.items():
        cap = min(n for _, n in entries)
        holders = usage.holders(key)
        if len(holders) > cap:
            errors.append('model %s: %d slots (%s) exceed max_concurrent %d'
                          % (key, len(holders), ', '.join(holders), cap))
    return errors
