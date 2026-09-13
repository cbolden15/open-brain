"""Pure compartment policy for Brain Protocol v1."""

from __future__ import annotations

import re
from collections.abc import Iterable

_COMPARTMENT = re.compile(r"[a-z][a-z0-9.-]{0,63}")


def canonical_compartments(labels: Iterable[str]) -> tuple[str, ...]:
    """Validate one exact label set and return its deterministic representation."""
    raw = tuple(labels)
    if not raw:
        raise ValueError("compartments must not be empty")
    if any(not isinstance(label, str) or _COMPARTMENT.fullmatch(label) is None for label in raw):
        raise ValueError("compartments contain an invalid label")
    if len(set(raw)) != len(raw):
        raise ValueError("compartments must be unique")
    return tuple(sorted(raw))


def propagated_compartments(*source_compartments: Iterable[str]) -> tuple[str, ...]:
    """Return the deterministic union required by every source."""
    result: set[str] = set()
    for labels in source_compartments:
        canonical = canonical_compartments(labels)
        result.update(canonical)
    if not result:
        raise ValueError("derived output requires at least one source compartment")
    return tuple(sorted(result))


def authorize_compartments(
    granted: Iterable[str],
    required: Iterable[str],
    *,
    spaces: Iterable[str] = (),
) -> bool:
    """Require every compartment; spaces are organization and never authority."""
    del spaces
    granted_set = set(canonical_compartments(granted))
    required_set = set(canonical_compartments(required))
    return required_set.issubset(granted_set)
