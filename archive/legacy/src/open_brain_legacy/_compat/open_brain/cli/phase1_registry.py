# Private legacy compatibility snapshot; excluded from every shipping artifact.
"""Minimal registry for the retained Phase 1 command families."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from open_brain_legacy._compat.open_brain.cli._common import CommandFamilyAdapter

_PHASE1_COMMANDS = frozenset({"capture", "inbox", "proposals", "query", "review", "spaces"})


@dataclass(frozen=True, slots=True)
class Phase1CommandAdapterRegistry:
    """Immutable task adapters for the six public Phase 1 command families."""

    adapters: Mapping[str, CommandFamilyAdapter]

    def __post_init__(self) -> None:
        if set(self.adapters) != _PHASE1_COMMANDS:
            raise ValueError("invalid Phase 1 command registry")
        object.__setattr__(self, "adapters", MappingProxyType(dict(self.adapters)))

    def get(self, name: str) -> CommandFamilyAdapter | None:
        return self.adapters.get(name)
