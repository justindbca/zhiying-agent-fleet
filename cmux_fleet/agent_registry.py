"""Offline registry of agent profiles used by the dynamic routing MVP.

Agent profile ids describe capabilities and launch preferences.  They are not fleet
slot ids: assigning ``flashnext-coder`` to ``worker-1`` must not rename the slot.
"""

from dataclasses import asdict, dataclass
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple, Union


CARD_SUFFIXES = (".json", ".yaml", ".yml")
AGENT_ID_RE = re.compile(r"[a-z][a-z0-9-]{0,63}$")
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_DIR = PACKAGE_DIR.parent


class AgentRegistryError(ValueError):
    """An agent card or registry directory is invalid."""


@dataclass(frozen=True)
class AgentCard:
    id: str
    display_name: str
    cli: str
    capabilities: Tuple[str, ...]
    role_file: Optional[str] = None
    cost_rank: Optional[int] = None
    latency_rank: Optional[int] = None
    risk_rank: Optional[int] = None
    model: Optional[str] = None
    profile: Optional[str] = None
    # Advisor whitelist of worker-kit domains for this card; None = no restriction.
    kit_domains: Optional[Tuple[str, ...]] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        if self.kit_domains is not None:
            data["kit_domains"] = list(self.kit_domains)
        return data


def _default_directory() -> Path:
    """Prefer a checkout-level agents directory, then packaged agent data."""
    for directory in (PROJECT_DIR / "agents", PACKAGE_DIR / "agents"):
        if directory.is_dir() and any(
                path.is_file() and path.suffix.lower() in CARD_SUFFIXES
                for path in directory.iterdir()):
            return directory
    return PACKAGE_DIR / "agents"


def _read_card(path: Path) -> Mapping[str, Any]:
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            try:
                import yaml  # type: ignore
            except ImportError as exc:
                raise AgentRegistryError(
                    "%s needs PyYAML; use JSON cards when PyYAML is unavailable" % path
                ) from exc
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except AgentRegistryError:
        raise
    except Exception as exc:
        raise AgentRegistryError("could not load agent card %s: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise AgentRegistryError("agent card %s must contain one object" % path)
    return data


def _required_string(data: Mapping[str, Any], field: str, path: Path) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise AgentRegistryError("agent card %s needs a non-empty %s" % (path, field))
    return value.strip()


def _safe_role_file(value: Any, path: Path) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        raise AgentRegistryError("agent card %s role_file must be a relative path" % path)
    value = value.strip()
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (posix.is_absolute() or windows.is_absolute() or windows.drive
            or ".." in posix.parts or value.startswith("~")):
        raise AgentRegistryError("agent card %s role_file must be relative and stay inside its registry" % path)
    return value


def _optional_rank(data: Mapping[str, Any], field: str, path: Path) -> Optional[int]:
    value = data.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise AgentRegistryError("agent card %s %s must be a non-negative integer" % (path, field))
    return value


def _optional_launch_string(data: Mapping[str, Any], field: str, path: Path) -> Optional[str]:
    value = data.get(field)
    if value is None:
        return None
    normalized = _required_string(data, field, path)
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise AgentRegistryError("agent card %s %s must not contain control characters" % (path, field))
    return normalized


def _optional_kit(data: Mapping[str, Any], path: Path) -> Optional[Tuple[str, ...]]:
    kit = data.get("kit")
    if kit is None:
        return None
    domains = kit.get("domains") if isinstance(kit, dict) and set(kit) <= {"domains"} else None
    if (not isinstance(domains, list)
            or not all(isinstance(item, str) and re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", item) for item in domains)):
        raise AgentRegistryError("agent card %s kit must be {domains: [domain names]}" % path)
    return tuple(dict.fromkeys(domains))


def _card_from_data(data: Mapping[str, Any], path: Path) -> AgentCard:
    agent_id = _required_string(data, "id", path)
    if not AGENT_ID_RE.fullmatch(agent_id):
        raise AgentRegistryError("agent card %s id must match %s" % (path, AGENT_ID_RE.pattern))
    capabilities = data.get("capabilities")
    if (not isinstance(capabilities, list) or not capabilities
            or not all(isinstance(item, str) and item.strip() for item in capabilities)):
        raise AgentRegistryError("agent card %s capabilities must be a non-empty string list" % path)
    normalized = tuple(dict.fromkeys(item.strip() for item in capabilities))
    return AgentCard(
        id=agent_id,
        display_name=_required_string(data, "display_name", path),
        cli=_required_string(data, "cli", path),
        capabilities=normalized,
        role_file=_safe_role_file(data.get("role_file"), path),
        cost_rank=_optional_rank(data, "cost_rank", path),
        latency_rank=_optional_rank(data, "latency_rank", path),
        risk_rank=_optional_rank(data, "risk_rank", path),
        model=_optional_launch_string(data, "model", path),
        profile=_optional_launch_string(data, "profile", path),
        kit_domains=_optional_kit(data, path),
    )


class AgentRegistry:
    """A deterministic, side-effect-free collection of validated agent cards."""

    def __init__(self, directory: Optional[Union[str, Path]] = None):
        self.directory = Path(directory).expanduser() if directory is not None else _default_directory()
        self._cards = self._load(self.directory)

    @staticmethod
    def _load(directory: Path) -> Tuple[AgentCard, ...]:
        if not directory.is_dir():
            raise AgentRegistryError("agent registry directory not found: %s" % directory)
        paths = sorted(
            (path for path in directory.iterdir()
             if path.is_file() and path.suffix.lower() in CARD_SUFFIXES),
            key=lambda path: path.name,
        )
        if not paths:
            raise AgentRegistryError("agent registry contains no JSON or YAML cards: %s" % directory)
        cards: List[AgentCard] = []
        seen = set()
        for path in paths:
            card = _card_from_data(_read_card(path), path)
            if card.id in seen:
                raise AgentRegistryError("duplicate agent id %s in %s" % (card.id, directory))
            seen.add(card.id)
            cards.append(card)
        return tuple(sorted(cards, key=lambda card: card.id))

    @property
    def cards(self) -> Tuple[AgentCard, ...]:
        return self._cards

    def get(self, agent_id: str) -> AgentCard:
        for card in self._cards:
            if card.id == agent_id:
                return card
        raise KeyError(agent_id)

    def as_list(self) -> List[Dict[str, Any]]:
        return [card.to_dict() for card in self._cards]

    def __iter__(self) -> Iterable[AgentCard]:
        return iter(self._cards)
