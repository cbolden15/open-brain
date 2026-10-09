"""Owner-selected source rules and provider eligibility for harness memory."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from open_brain_engine.core.models import PrivacyTier

_SCHEMA = "open-brain-harness-policy.v1"
_ID = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_CLOUD_TIERS = frozenset({PrivacyTier.PUBLIC, PrivacyTier.WORK, PrivacyTier.PERSONAL})
_LOCAL_MARKING = re.compile(r"(?im)^\s*(?:confidential|local[- ]only|private)\s*:")


class HarnessPolicyError(ValueError):
    """A metadata-only policy refusal."""

    def __init__(self, code: str = "invalid_policy") -> None:
        if code not in {"invalid_policy", "unsafe_policy", "source_excluded", "disabled"}:
            raise ValueError("invalid policy error")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class HarnessSourceRule:
    source_id: str
    project_root: Path
    tier: PrivacyTier
    local_only: bool


@dataclass(frozen=True, slots=True)
class HarnessSourceDecision:
    source_id: str | None
    tier: PrivacyTier
    local_only: bool


@dataclass(frozen=True, slots=True)
class HarnessPolicy:
    enabled: bool
    providers: tuple[tuple[str, frozenset[PrivacyTier]], ...]
    sources: tuple[HarnessSourceRule, ...]
    exclusions: tuple[Path, ...]

    @classmethod
    def from_dict(cls, value: object) -> HarnessPolicy:
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "enabled",
            "providers",
            "sources",
            "exclusions",
        }:
            raise HarnessPolicyError()
        if value["schema_version"] != _SCHEMA or type(value["enabled"]) is not bool:
            raise HarnessPolicyError()
        providers = value["providers"]
        sources = value["sources"]
        exclusions = value["exclusions"]
        if (
            not isinstance(providers, dict)
            or len(providers) > 32
            or not isinstance(sources, list)
            or len(sources) > 256
            or not isinstance(exclusions, list)
            or len(exclusions) > 256
        ):
            raise HarnessPolicyError()
        grants: list[tuple[str, frozenset[PrivacyTier]]] = []
        for provider, raw_tiers in providers.items():
            if not isinstance(provider, str) or _ID.fullmatch(provider) is None:
                raise HarnessPolicyError()
            if (
                not isinstance(raw_tiers, list)
                or not raw_tiers
                or len(raw_tiers) != len(set(str(tier) for tier in raw_tiers))
            ):
                raise HarnessPolicyError()
            tiers = frozenset(_tier(tier) for tier in raw_tiers)
            if not tiers <= _CLOUD_TIERS:
                raise HarnessPolicyError()
            grants.append((provider, tiers))
        rules: list[HarnessSourceRule] = []
        for source in sources:
            if not isinstance(source, dict) or set(source) != {
                "id",
                "project_root",
                "tier",
                "local_only",
            }:
                raise HarnessPolicyError()
            identity = source["id"]
            if (
                not isinstance(identity, str)
                or _ID.fullmatch(identity) is None
                or type(source["local_only"]) is not bool
            ):
                raise HarnessPolicyError()
            tier = _tier(source["tier"])
            rules.append(
                HarnessSourceRule(
                    identity,
                    _path(source["project_root"]),
                    tier,
                    source["local_only"] or tier not in _CLOUD_TIERS,
                )
            )
        if len({rule.source_id for rule in rules}) != len(rules) or len(
            {rule.project_root for rule in rules}
        ) != len(rules):
            raise HarnessPolicyError()
        paths = tuple(_path(path) for path in exclusions)
        if len(set(paths)) != len(paths):
            raise HarnessPolicyError()
        return cls(value["enabled"], tuple(sorted(grants)), tuple(rules), paths)

    def classify(self, project: Path, *, authored_text: str = "") -> HarnessSourceDecision:
        """Resolve an explicit path rule; text markings may only restrict it."""
        if not self.enabled:
            raise HarnessPolicyError("disabled")
        project = _path(project)
        if any(project.is_relative_to(path) for path in self.exclusions):
            raise HarnessPolicyError("source_excluded")
        candidates = [rule for rule in self.sources if project.is_relative_to(rule.project_root)]
        if not candidates:
            return HarnessSourceDecision(None, PrivacyTier.UNKNOWN, True)
        rule = max(candidates, key=lambda item: len(item.project_root.parts))
        marked = bool(_LOCAL_MARKING.search(authored_text))
        return HarnessSourceDecision(rule.source_id, rule.tier, rule.local_only or marked)

    def allowed_tiers(self, provider: str) -> frozenset[PrivacyTier]:
        """Unknown or revoked providers receive no cloud grant."""
        if not self.enabled:
            return frozenset()
        return next((tiers for name, tiers in self.providers if name == provider), frozenset())

    def permits_provider(self, decision: HarnessSourceDecision, provider: str) -> bool:
        return not decision.local_only and decision.tier in self.allowed_tiers(provider)


def load_harness_policy(path: Path) -> HarnessPolicy:
    """Read one bounded owner-only file without following a swapped symlink."""
    if not path.is_absolute():
        raise HarnessPolicyError("unsafe_policy")
    descriptor: int | None = None
    try:
        parent = path.parent.stat(follow_symlinks=False)
        if (
            not stat.S_ISDIR(parent.st_mode)
            or parent.st_uid != os.geteuid()
            or stat.S_IMODE(parent.st_mode) & 0o077
        ):
            raise HarnessPolicyError("unsafe_policy")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > 65536
        ):
            raise HarnessPolicyError("unsafe_policy")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise HarnessPolicyError()
        value = json.loads(raw, object_pairs_hook=_unique_object)
        return HarnessPolicy.from_dict(value)
    except HarnessPolicyError:
        raise
    except OSError, ValueError, TypeError, UnicodeError:
        raise HarnessPolicyError("unsafe_policy") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _unique_object(pairs: list[tuple[str, object]]) -> Mapping[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise HarnessPolicyError()
        value[key] = item
    return value


def _tier(value: object) -> PrivacyTier:
    try:
        if not isinstance(value, str):
            raise ValueError
        return PrivacyTier(value)
    except ValueError:
        raise HarnessPolicyError() from None


def _path(value: object) -> Path:
    if not isinstance(value, (str, Path)):
        raise HarnessPolicyError()
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\0" in str(path):
        raise HarnessPolicyError()
    try:
        return path.resolve(strict=True)
    except OSError:
        raise HarnessPolicyError() from None
