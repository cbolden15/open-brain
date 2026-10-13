"""Pure, owner-safe pieces of managed facet projection.

Filesystem custody and semantic assignment remain with the engine integration.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import PurePosixPath
from urllib.parse import quote

from open_brain_engine.storage.markdown import MarkdownFormatError, parse_markdown


class FacetProjectionConflict(ValueError):
    """A generated projection would claim or overwrite owner content."""


@dataclass(frozen=True, slots=True)
class ProjectedValue:
    dimension_id: str
    dimension_name: str
    value_id: str
    value_name: str
    effectively_archived: bool = False


@dataclass(frozen=True, slots=True)
class ReconciliationConflict:
    code: str
    value_id: str | None = None


@dataclass(frozen=True, slots=True)
class Reconciliation:
    add_value_ids: tuple[str, ...]
    remove_value_ids: tuple[str, ...]
    conflicts: tuple[ReconciliationConflict, ...]


@dataclass(frozen=True, slots=True)
class IndexLink:
    label: str
    relative_path: str


def _key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def rendered_facet_map(values: Sequence[ProjectedValue]) -> dict[str, list[str]]:
    """Return the deterministic, quoted YAML-flow-compatible facet subtree."""
    result: dict[str, list[str]] = {}
    dimensions: dict[str, tuple[str, list[ProjectedValue]]] = {}
    seen: set[str] = set()
    for value in values:
        if value.value_id in seen:
            raise ValueError("duplicate facet value")
        seen.add(value.value_id)
        existing = dimensions.setdefault(value.dimension_id, (value.dimension_name, []))
        if existing[0] != value.dimension_name:
            raise ValueError("inconsistent dimension name")
        existing[1].append(value)
    for _dimension_id, (name, members) in sorted(
        dimensions.items(), key=lambda item: (_key(item[1][0]), item[0])
    ):
        if name in result:
            raise ValueError("duplicate rendered dimension")
        ordered = sorted(members, key=lambda member: (_key(member.value_name), member.value_id))
        labels = [member.value_name for member in ordered]
        if len(labels) != len(set(labels)):
            raise ValueError("duplicate rendered value")
        result[name] = labels
    return result


def replace_registered_facets(
    payload: bytes, values: Sequence[ProjectedValue], *, registered: bool
) -> bytes:
    """Change only the facets line in deterministic managed Markdown.

    An unregistered owner property is never adopted as generated metadata.
    The caller must separately guard file identity, digest, and projection custody.
    """
    parsed = parse_markdown(payload)
    if "facets" in parsed.fields and not registered:
        raise FacetProjectionConflict("unregistered owner facets")
    text = payload.decode("utf-8")
    frontmatter, separator, body = text.partition("\n---\n\n")
    if not separator:
        raise MarkdownFormatError("invalid Markdown frontmatter")
    lines = frontmatter.split("\n")
    encoded = json.dumps(rendered_facet_map(values), ensure_ascii=False, separators=(",", ":"))
    line = f"facets: {encoded}"
    existing = [index for index, item in enumerate(lines) if item.startswith("facets: ")]
    if existing:
        lines[existing[0]] = line
    else:
        lines.append(line)
    result = ("\n".join(lines) + separator + body).encode("utf-8")
    parse_markdown(result)
    return result


def content_without_registered_facets(payload: bytes, *, registered: bool) -> bytes:
    """Recover content bytes for revision/evidence comparison only for a known projection."""
    parsed = parse_markdown(payload)
    if not registered or "facets" not in parsed.fields:
        return payload
    text = payload.decode("utf-8")
    frontmatter, separator, body = text.partition("\n---\n\n")
    lines = [line for line in frontmatter.split("\n") if not line.startswith("facets: ")]
    result = ("\n".join(lines) + separator + body).encode("utf-8")
    parse_markdown(result)
    return result


def reconcile_facets(
    proposed: Mapping[str, object] | None,
    *,
    previous: Sequence[ProjectedValue],
    vocabulary: Sequence[ProjectedValue],
    current_memberships: frozenset[str],
) -> Reconciliation:
    """Resolve labels against retained identity and current vocabulary.

    Missing or empty facets propose removal of previously projected memberships.
    Neither this function nor an observer commits classification.
    """
    aliases: dict[tuple[str, str], set[str]] = {}
    current_by_id = {value.value_id: value for value in vocabulary}
    if len(current_by_id) != len(vocabulary):
        raise ValueError("duplicate vocabulary ID")
    for value in (*previous, *vocabulary):
        aliases.setdefault((_key(value.dimension_name), _key(value.value_name)), set()).add(
            value.value_id
        )
    desired: set[str] = set()
    conflicts: list[ReconciliationConflict] = []
    if proposed is not None:
        if not isinstance(proposed, Mapping):
            return Reconciliation((), (), (ReconciliationConflict("invalid_frontmatter"),))
        for dimension_name, raw_values in proposed.items():
            if not isinstance(dimension_name, str) or not isinstance(raw_values, list):
                return Reconciliation((), (), (ReconciliationConflict("invalid_frontmatter"),))
            for value_name in raw_values:
                if not isinstance(value_name, str):
                    return Reconciliation((), (), (ReconciliationConflict("invalid_frontmatter"),))
                candidates = aliases.get((_key(dimension_name), _key(value_name)), set())
                if not candidates:
                    conflicts.append(ReconciliationConflict("unknown_name"))
                elif len(candidates) != 1:
                    conflicts.append(ReconciliationConflict("ambiguous_name"))
                else:
                    value_id = next(iter(candidates))
                    if value_id in desired:
                        conflicts.append(ReconciliationConflict("invalid_frontmatter"))
                    else:
                        desired.add(value_id)
    additions = desired - current_memberships
    for value_id in sorted(additions):
        current_value = current_by_id.get(value_id)
        if current_value is None:
            conflicts.append(ReconciliationConflict("unknown_name"))
        elif current_value.effectively_archived:
            conflicts.append(ReconciliationConflict("archived_addition", value_id))
    prior_ids = {value.value_id for value in previous}
    return Reconciliation(
        tuple(sorted(additions & current_by_id.keys())),
        tuple(sorted((prior_ids & current_memberships) - desired)),
        tuple(conflicts),
    )


def _markdown_text(value: str) -> str:
    return "".join("\\" + char if char in "\\`*_{}[]<>()#+-.!|" else char for char in value)


def facet_index_relative_path(dimension_id: str, value_id: str) -> str:
    """Use immutable IDs, never display names, for an index path."""
    uuid = r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
    if not re.fullmatch("facetdim_" + uuid, dimension_id) or not re.fullmatch(
        "facetval_" + uuid, value_id
    ):
        raise ValueError("invalid facet index identity")
    return f"Facets/{dimension_id}/{value_id}.md"


def render_facet_index(
    *, title: str, effectively_archived: bool, links: Sequence[IndexLink]
) -> bytes:
    """Render deterministic navigation, with no source-note or evidence semantics."""
    if any(ord(char) < 32 for char in title):
        raise ValueError("unsafe index title")
    by_path: dict[str, IndexLink] = {}
    for link in links:
        path = PurePosixPath(link.relative_path)
        if (
            path.is_absolute()
            or not path.parts
            or str(path) != link.relative_path
            or any(part in {".", ".."} for part in path.parts)
            or any(ord(char) < 32 for char in link.relative_path + link.label)
        ):
            raise ValueError("unsafe index link")
        by_path.setdefault(link.relative_path, link)
    heading = _markdown_text(title)
    if effectively_archived:
        heading += " (archived)"
    lines = [f"# {heading}", ""]
    for link in sorted(by_path.values(), key=lambda item: (_key(item.label), item.relative_path)):
        target = "../../" + quote(link.relative_path, safe="/-._~")
        lines.append(f"- [{_markdown_text(link.label)}]({target})")
    lines.append("")
    return "\n".join(lines).encode("utf-8")


def index_write_status(
    *, previous_digest: str | None, existing: bytes | None, proposed: bytes
) -> str:
    """Return write, current, or conflict without discarding an owner edit."""
    if existing is not None and (
        previous_digest is None or sha256(existing).hexdigest() != previous_digest
    ):
        return "conflict"
    return "current" if existing == proposed else "write"
