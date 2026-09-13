"""Immutable, local-first application configuration."""

from __future__ import annotations

import os
import re
import tomllib
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final


class ConfigError(ValueError):
    """Raised when configuration cannot be validated safely."""


class SecretResolutionError(ConfigError):
    """Raised when an external secret cannot be resolved safely."""


class SecretRefKind(StrEnum):
    """Closed secret-reference source kinds."""

    ENVIRONMENT = "env"
    FILE = "file"


_MISSING: Final = object()
_ENV_FIELDS: Final = {
    "state_root": "OPEN_BRAIN_STATE_ROOT",
    "work_root": "OPEN_BRAIN_WORK_ROOT",
    "personal_root": "OPEN_BRAIN_PERSONAL_ROOT",
    "capture_root": "OPEN_BRAIN_CAPTURE_ROOT",
    "saved_content_root": "OPEN_BRAIN_SAVED_CONTENT_ROOT",
    "backup_root": "OPEN_BRAIN_BACKUP_ROOT",
    "host_identity": "OPEN_BRAIN_HOST_IDENTITY",
    "provider": "OPEN_BRAIN_PROVIDER",
    "cloud_enabled": "OPEN_BRAIN_CLOUD_ENABLED",
    "egress_enabled": "OPEN_BRAIN_EGRESS_ENABLED",
}
_TOML_FIELDS: Final = {
    "state_root": ("paths", "state_root"),
    "work_root": ("paths", "work_root"),
    "personal_root": ("paths", "personal_root"),
    "capture_root": ("paths", "capture_root"),
    "saved_content_root": ("paths", "saved_content_root"),
    "backup_root": ("paths", "backup_root"),
    "host_identity": ("host", "identity"),
    "provider": ("providers", "default"),
    "cloud_enabled": ("providers", "cloud_enabled"),
    "egress_enabled": ("egress", "enabled"),
}
_DEFAULTS: Final = {
    "host_identity": None,
    "provider": "local",
    "cloud_enabled": False,
    "egress_enabled": False,
}
_ALLOWED_TOML_KEYS: Final = {
    "paths": frozenset(
        {
            "work_root",
            "personal_root",
            "capture_root",
            "saved_content_root",
            "state_root",
            "backup_root",
        }
    ),
    "providers": frozenset({"default", "cloud_enabled"}),
    "egress": frozenset({"enabled"}),
    "host": frozenset({"identity"}),
    "ledger": frozenset({"taxonomy"}),
}
_PROVIDER_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ENVIRONMENT_NAME_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_NAME_RE: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_IDENTITY_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")
_HOST_IDENTITY_RE: Final = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_TRUE_VALUES: Final = frozenset({"true"})
_FALSE_VALUES: Final = frozenset({"false"})


@dataclass(frozen=True, slots=True)
class SecretRef:
    """Validated reference to a secret held outside application configuration."""

    kind: SecretRefKind
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, SecretRefKind) or not isinstance(self.value, str):
            raise ConfigError("invalid secret reference")
        if self.kind is SecretRefKind.ENVIRONMENT:
            if not _ENVIRONMENT_NAME_RE.fullmatch(self.value):
                raise ConfigError("invalid secret environment reference")
            return
        if (
            not self.value
            or "~" in self.value
            or self.value.startswith("//")
            or any(ord(char) < 32 for char in self.value)
            or not Path(self.value).is_absolute()
        ):
            raise ConfigError("invalid secret file reference")

    @classmethod
    def parse(cls, value: str) -> SecretRef:
        """Parse an ``env:NAME`` or ``file:/absolute/path`` reference."""
        if not isinstance(value, str) or ":" not in value:
            raise ConfigError("invalid secret reference")
        raw_kind, target = value.split(":", 1)
        try:
            kind = SecretRefKind(raw_kind)
        except ValueError:
            raise ConfigError("unsupported secret reference scheme") from None
        return cls(kind=kind, value=target)

    def to_string(self) -> str:
        """Return the typed non-secret reference string."""
        return f"{self.kind.value}:{self.value}"


@dataclass(frozen=True, slots=True)
class NamedSecretRef:
    """A stable public configuration name bound to an external secret reference."""

    name: str
    reference: SecretRef

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SECRET_NAME_RE.fullmatch(self.name):
            raise ConfigError("invalid secret reference name")
        if not isinstance(self.reference, SecretRef):
            raise ConfigError("invalid secret reference")


def _named_secret_refs(value: object, *, source: str) -> tuple[NamedSecretRef, ...]:
    section = _as_mapping(value, f"{source} [secrets]")
    references: list[NamedSecretRef] = []
    for name in sorted(section):
        raw_reference = section[name]
        if isinstance(raw_reference, SecretRef) and source == "explicit":
            reference = raw_reference
        elif isinstance(raw_reference, str):
            reference = SecretRef.parse(raw_reference)
        else:
            raise ConfigError("invalid secret reference")
        references.append(NamedSecretRef(name, reference))
    return tuple(references)


def resolve_secret(
    reference: SecretRef,
    *,
    environment: Mapping[str, str],
    file_reader: Callable[[Path], str],
) -> str:
    """Resolve a secret for immediate caller use without retaining or logging it."""
    if not isinstance(reference, SecretRef):
        raise SecretResolutionError("invalid secret reference")

    value: object = _MISSING
    if reference.kind is SecretRefKind.ENVIRONMENT:
        try:
            value = environment.get(reference.value, _MISSING)
        except Exception:
            value = _MISSING
        if not isinstance(value, str) or value == "":
            raise SecretResolutionError("secret environment value unavailable")
        return value

    try:
        value = file_reader(Path(reference.value))
    except Exception:
        value = _MISSING
    if not isinstance(value, str) or value == "":
        raise SecretResolutionError("secret file value unavailable")
    return value


def _as_mapping(value: object, source: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{source} must be a table")
    if not all(isinstance(key, str) for key in value):
        raise ConfigError(f"{source} keys must be strings")
    return value


def _flatten_explicit(explicit: Mapping[str, object]) -> dict[str, object]:
    aliases = {
        "state_root": "state_root",
        "work_root": "work_root",
        "personal_root": "personal_root",
        "capture_root": "capture_root",
        "saved_content_root": "saved_content_root",
        "backup_root": "backup_root",
        "host_identity": "host_identity",
        "provider": "provider",
        "default": "provider",
        "cloud_enabled": "cloud_enabled",
        "egress_enabled": "egress_enabled",
        "paths.state_root": "state_root",
        "paths.work_root": "work_root",
        "paths.personal_root": "personal_root",
        "paths.capture_root": "capture_root",
        "paths.saved_content_root": "saved_content_root",
        "paths.backup_root": "backup_root",
        "host.identity": "host_identity",
        "providers.default": "provider",
        "providers.cloud_enabled": "cloud_enabled",
        "egress.enabled": "egress_enabled",
    }
    flattened: dict[str, object] = {}

    for key, value in explicit.items():
        if key == "secrets":
            flattened["secret_refs"] = _named_secret_refs(value, source="explicit")
            continue
        if key == "ledger":
            section = _as_mapping(value, "explicit ledger")
            if set(section) != {"taxonomy"}:
                raise ConfigError("invalid explicit ledger configuration")
            flattened["ledger"] = _ledger_config(section["taxonomy"])
            continue
        if key in _ALLOWED_TOML_KEYS:
            section = _as_mapping(value, f"explicit {key}")
            for nested_key, nested_value in section.items():
                alias = aliases.get(f"{key}.{nested_key}")
                if alias is None:
                    raise ConfigError(f"unknown explicit key: {key}.{nested_key}")
                if alias in flattened:
                    raise ConfigError(f"duplicate explicit key: {alias}")
                flattened[alias] = nested_value
            continue

        alias = aliases.get(key)
        if alias is None:
            raise ConfigError(f"unknown explicit key: {key}")
        if alias in flattened:
            raise ConfigError(f"duplicate explicit key: {alias}")
        flattened[alias] = value

    return flattened


def _validate_toml(data: Mapping[str, object]) -> dict[str, object]:
    values: dict[str, object] = {}
    if "secrets" in data:
        values["secret_refs"] = _named_secret_refs(data["secrets"], source="TOML")
    for section_name, allowed_keys in _ALLOWED_TOML_KEYS.items():
        if section_name not in data:
            continue
        section = _as_mapping(data[section_name], f"TOML [{section_name}]")
        unknown = set(section) - allowed_keys
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ConfigError(f"unknown TOML key(s) in [{section_name}]: {names}")
        if section_name == "ledger":
            if "taxonomy" in section:
                values["ledger"] = _ledger_config(section["taxonomy"])
            continue
        for key, value in section.items():
            field = next(
                field_name
                for field_name, location in _TOML_FIELDS.items()
                if location == (section_name, key)
            )
            if field in values:
                raise ConfigError(f"duplicate TOML key: {section_name}.{key}")
            values[field] = value

    unknown_sections = set(data) - set(_ALLOWED_TOML_KEYS) - {"secrets"}
    if unknown_sections:
        names = ", ".join(sorted(unknown_sections))
        raise ConfigError(f"unknown TOML section(s): {names}")
    return values


_LEDGER_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_LEDGER_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_UNSAFE_LEDGER_LABEL = re.compile(r"[\#>*_`\[\]()<>|]")
_LEDGER_PRIVACY_TIERS = frozenset({"public", "work", "personal"})


def _ledger_text(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"invalid {field_name}")
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or normalized.isspace()
        or any(ord(character) < 32 for character in normalized)
    ):
        raise ConfigError(f"invalid {field_name}")
    return normalized


@dataclass(frozen=True, slots=True)
class LedgerRouteConfig:
    """Application-owned configuration for one ledger taxonomy route."""

    path_prefix: tuple[str, ...]
    topic_id: str
    topic_label: str
    privacy_tier: str | None

    @classmethod
    def create(
        cls,
        *,
        path_prefix: tuple[str, ...],
        topic_id: str,
        topic_label: str,
        privacy_tier: str | None,
    ) -> LedgerRouteConfig:
        if not isinstance(path_prefix, tuple) or not path_prefix:
            raise ConfigError("invalid ledger path prefix")
        normalized_prefix = tuple(
            _ledger_text(component, field_name="ledger path component")
            for component in path_prefix
        )
        if any(
            component in {".", ".."} or "/" in component or "\\" in component
            for component in normalized_prefix
        ):
            raise ConfigError("invalid ledger path prefix")
        normalized_id = _ledger_text(topic_id, field_name="ledger topic ID")
        if not _LEDGER_IDENTIFIER.fullmatch(normalized_id):
            raise ConfigError("invalid ledger topic ID")
        normalized_label = _ledger_text(topic_label, field_name="ledger topic label")
        if (
            len(normalized_label) > 128
            or any(not character.isprintable() for character in normalized_label)
            or _UNSAFE_LEDGER_LABEL.search(normalized_label)
        ):
            raise ConfigError("invalid ledger topic label")
        if privacy_tier is not None and privacy_tier not in _LEDGER_PRIVACY_TIERS:
            raise ConfigError("invalid ledger route privacy tier")
        return cls(normalized_prefix, normalized_id, normalized_label, privacy_tier)

    def to_dict(self) -> dict[str, object]:
        return {
            "path_prefix": list(self.path_prefix),
            "topic_id": self.topic_id,
            "topic_label": self.topic_label,
            "privacy_tier": self.privacy_tier,
        }


@dataclass(frozen=True, slots=True)
class LedgerTaxonomyConfig:
    """Application-owned immutable ledger taxonomy configuration."""

    version: str
    routes: tuple[LedgerRouteConfig, ...]

    @classmethod
    def create(
        cls,
        *,
        version: str,
        routes: tuple[LedgerRouteConfig, ...],
    ) -> LedgerTaxonomyConfig:
        if not isinstance(version, str) or _LEDGER_VERSION.fullmatch(version) is None:
            raise ConfigError("invalid ledger taxonomy version")
        if not isinstance(routes, tuple) or any(
            not isinstance(route, LedgerRouteConfig) for route in routes
        ):
            raise ConfigError("invalid ledger taxonomy routes")
        prefixes = tuple(route.path_prefix for route in routes)
        if len(prefixes) != len(set(prefixes)):
            raise ConfigError("ambiguous ledger taxonomy routes")
        return cls(version, tuple(sorted(routes, key=lambda route: route.path_prefix)))

    @classmethod
    def empty(cls) -> LedgerTaxonomyConfig:
        return cls.create(version="ledger-v1", routes=())

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "routes": [route.to_dict() for route in self.routes]}


@dataclass(frozen=True, slots=True)
class LedgerConfig:
    """Immutable, fail-closed application configuration for ledger routing."""

    taxonomy: LedgerTaxonomyConfig = field(default_factory=LedgerTaxonomyConfig.empty)

    def __post_init__(self) -> None:
        if not isinstance(self.taxonomy, LedgerTaxonomyConfig):
            raise ConfigError("invalid ledger configuration")

    def to_dict(self) -> dict[str, object]:
        return {"taxonomy": self.taxonomy.to_dict()}


def _ledger_config(value: object) -> LedgerConfig:
    """Return a safe empty taxonomy whenever untrusted configuration is unusable."""
    try:
        if not isinstance(value, Mapping) or set(value) != {"version", "routes"}:
            raise ConfigError("invalid ledger taxonomy")
        version = value["version"]
        routes_value = value["routes"]
        if not isinstance(routes_value, list):
            raise ConfigError("invalid ledger taxonomy routes")
        routes = tuple(_ledger_route(route) for route in routes_value)
        return LedgerConfig(taxonomy=LedgerTaxonomyConfig.create(version=version, routes=routes))
    except (KeyError, ConfigError, TypeError, ValueError):
        return LedgerConfig()


def _ledger_route(value: object) -> LedgerRouteConfig:
    if not isinstance(value, Mapping) or set(value) != {
        "path_prefix",
        "topic_id",
        "topic_label",
        "privacy_tier",
    }:
        raise ConfigError("invalid ledger route")
    raw_prefix = value["path_prefix"]
    if not isinstance(raw_prefix, list):
        raise ConfigError("invalid ledger route path prefix")
    return LedgerRouteConfig.create(
        path_prefix=tuple(raw_prefix),
        topic_id=value["topic_id"],
        topic_label=value["topic_label"],
        privacy_tier=value["privacy_tier"],
    )


def _read_toml(path: Path) -> dict[str, object]:
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("unable to read configuration file") from exc
    return _validate_toml(data)


def _parse_boolean(value: object, field: str, source: str) -> bool:
    if isinstance(value, bool):
        return value
    if source == "TOML":
        raise ConfigError(f"{field} in TOML must be a boolean")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    raise ConfigError(f"{field} from {source} must be true or false")


def _parse_root(value: object, field: str) -> Path:
    if isinstance(value, Path):
        text = os.fspath(value)
    elif isinstance(value, str):
        text = value
    else:
        raise ConfigError(f"{field} must be an absolute path")

    if not text or "~" in text or any(ord(char) < 32 for char in text):
        raise ConfigError(f"{field} is not a safe absolute path")
    path = Path(text)
    if not path.is_absolute():
        raise ConfigError(f"{field} must be absolute")
    return Path(os.path.normpath(text))


@dataclass(frozen=True, slots=True)
class RetainedRoots:
    """Existing content roots plus a distinct runtime-state root."""

    work: Path
    personal: Path
    capture: Path
    saved_content: Path
    state: Path

    def __post_init__(self) -> None:
        names = ("work", "personal", "capture", "saved_content", "state")
        for name in names:
            object.__setattr__(self, name, _parse_root(getattr(self, name), f"{name}_root"))
        roots = self.as_tuple()
        if any(
            _paths_overlap(left, right)
            for index, left in enumerate(roots)
            for right in roots[index + 1 :]
        ):
            raise ConfigError("retained roots must be distinct and non-overlapping")

    def as_tuple(self) -> tuple[Path, ...]:
        """Return roots in stable content-then-state order."""
        return (self.work, self.personal, self.capture, self.saved_content, self.state)

    def to_dict(self) -> dict[str, str]:
        """Return the public configuration key shape without relocating roots."""
        return {
            "work_root": os.fspath(self.work),
            "personal_root": os.fspath(self.personal),
            "capture_root": os.fspath(self.capture),
            "saved_content_root": os.fspath(self.saved_content),
            "state_root": os.fspath(self.state),
        }

    def validate_identities(self, identities: RetainedRootIdentities) -> None:
        """Reject injected physical aliases without resolving host paths."""
        if not isinstance(identities, RetainedRootIdentities):
            raise ConfigError("invalid retained root identities")
        if len(set(identities.as_tuple())) != len(self.as_tuple()):
            raise ConfigError("retained root identities must be distinct")


@dataclass(frozen=True, slots=True)
class RetainedRootIdentities:
    """Injected opaque physical identities for retained roots."""

    work: str
    personal: str
    capture: str
    saved_content: str
    state: str

    def __post_init__(self) -> None:
        if any(
            not isinstance(identity, str) or not _IDENTITY_RE.fullmatch(identity)
            for identity in self.as_tuple()
        ):
            raise ConfigError("invalid retained root identity")

    def as_tuple(self) -> tuple[str, ...]:
        return (self.work, self.personal, self.capture, self.saved_content, self.state)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _parse_provider(value: object) -> str:
    if not isinstance(value, str) or not _PROVIDER_RE.fullmatch(value):
        raise ConfigError("provider must be a non-empty provider name")
    return value


def _parse_host_identity(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _HOST_IDENTITY_RE.fullmatch(value) is None:
        raise ConfigError("invalid host identity")
    return value


def _config_path(value: object) -> Path:
    if not isinstance(value, (str, Path)):
        raise ConfigError("configuration path must be absolute")
    path = Path(value)
    if "~" in os.fspath(path) or not path.is_absolute():
        raise ConfigError("configuration path must be absolute and must not use ~")
    return path


def _select(
    field: str,
    explicit: Mapping[str, object],
    environment: Mapping[str, object],
    toml_values: Mapping[str, object],
) -> tuple[object, str]:
    if field in explicit:
        return explicit[field], "explicit configuration"
    env_name = _ENV_FIELDS[field]
    if env_name in environment:
        return environment[env_name], "environment"
    if field in toml_values:
        return toml_values[field], "TOML"
    if field in _DEFAULTS:
        return _DEFAULTS[field], "default"
    raise ConfigError(f"{field} is required")


@dataclass(frozen=True, slots=True)
class AppConfig:
    """Validated configuration with no secret-bearing or ambient inputs."""

    roots: RetainedRoots
    backup: Path
    host_identity: str | None = None
    provider: str = "local"
    cloud_enabled: bool = False
    egress_enabled: bool = False
    ledger: LedgerConfig = field(default_factory=LedgerConfig)
    secret_refs: tuple[NamedSecretRef, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.roots, RetainedRoots):
            raise ConfigError("invalid retained roots")
        backup = _parse_root(self.backup, "backup_root")
        if any(_paths_overlap(backup, retained) for retained in self.roots.as_tuple()):
            raise ConfigError("backup root must be distinct and non-overlapping")
        object.__setattr__(self, "backup", backup)
        object.__setattr__(self, "host_identity", _parse_host_identity(self.host_identity))
        object.__setattr__(self, "provider", _parse_provider(self.provider))
        if type(self.cloud_enabled) is not bool or type(self.egress_enabled) is not bool:
            raise ConfigError("cloud_enabled and egress_enabled must be booleans")
        if not isinstance(self.ledger, LedgerConfig):
            raise ConfigError("invalid ledger configuration")
        if not isinstance(self.secret_refs, tuple) or any(
            not isinstance(reference, NamedSecretRef) for reference in self.secret_refs
        ):
            raise ConfigError("invalid secret references")
        names = [reference.name for reference in self.secret_refs]
        if names != sorted(set(names)):
            raise ConfigError("secret references must have distinct sorted names")

    @property
    def work_root(self) -> Path:
        return self.roots.work

    @property
    def personal_root(self) -> Path:
        return self.roots.personal

    @property
    def capture_root(self) -> Path:
        return self.roots.capture

    @property
    def saved_content_root(self) -> Path:
        return self.roots.saved_content

    @property
    def state_root(self) -> Path:
        return self.roots.state

    @property
    def backup_root(self) -> Path:
        return self.backup

    @classmethod
    def from_sources(
        cls,
        *,
        explicit: Mapping[str, object] | None = None,
        environment: Mapping[str, object] | None = None,
        config_path: str | Path | None = None,
        toml_data: Mapping[str, object] | None = None,
    ) -> AppConfig:
        """Build configuration using only the supplied source mappings and file."""
        explicit_values = _flatten_explicit(explicit or {})
        environment_values: Mapping[str, object] = environment or {}

        if "OPEN_BRAIN_CONFIG" in environment_values and config_path is not None:
            raise ConfigError("configuration path supplied twice")
        selected_path = config_path
        if selected_path is None and "OPEN_BRAIN_CONFIG" in environment_values:
            selected_path = environment_values["OPEN_BRAIN_CONFIG"]  # type: ignore[assignment]
        if selected_path is not None and toml_data is not None:
            raise ConfigError("TOML supplied twice")
        if toml_data is not None:
            toml_values = _validate_toml(toml_data)
        elif selected_path is not None:
            toml_values = _read_toml(_config_path(selected_path))
        else:
            toml_values = {}

        selected = {
            field: _select(field, explicit_values, environment_values, toml_values)
            for field in _ENV_FIELDS
        }
        roots = RetainedRoots(
            work=_parse_root(selected["work_root"][0], "work_root"),
            personal=_parse_root(selected["personal_root"][0], "personal_root"),
            capture=_parse_root(selected["capture_root"][0], "capture_root"),
            saved_content=_parse_root(
                selected["saved_content_root"][0], "saved_content_root"
            ),
            state=_parse_root(selected["state_root"][0], "state_root"),
        )
        ledger = explicit_values.get("ledger", toml_values.get("ledger", LedgerConfig()))
        if not isinstance(ledger, LedgerConfig):
            raise ConfigError("invalid ledger configuration")
        secret_refs = explicit_values.get("secret_refs", toml_values.get("secret_refs", ()))
        if not isinstance(secret_refs, tuple):
            raise ConfigError("invalid secret references")
        return cls(
            roots=roots,
            backup=_parse_root(selected["backup_root"][0], "backup_root"),
            host_identity=_parse_host_identity(selected["host_identity"][0]),
            provider=_parse_provider(selected["provider"][0]),
            cloud_enabled=_parse_boolean(
                selected["cloud_enabled"][0], "cloud_enabled", selected["cloud_enabled"][1]
            ),
            egress_enabled=_parse_boolean(
                selected["egress_enabled"][0], "egress_enabled", selected["egress_enabled"][1]
            ),
            ledger=ledger,
            secret_refs=secret_refs,
        )

    @classmethod
    def load(
        cls,
        *,
        explicit: Mapping[str, object] | None = None,
        environment: Mapping[str, object] | None = None,
        config_path: str | Path | None = None,
        toml_data: Mapping[str, object] | None = None,
    ) -> AppConfig:
        """Alias for :meth:`from_sources` for composition roots."""
        return cls.from_sources(
            explicit=explicit,
            environment=environment,
            config_path=config_path,
            toml_data=toml_data,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the non-secret public configuration shape."""
        return {
            "paths": {
                **self.roots.to_dict(),
                "backup_root": os.fspath(self.backup),
            },
            "providers": {
                "default": self.provider,
                "cloud_enabled": self.cloud_enabled,
            },
            "egress": {"enabled": self.egress_enabled},
            "host": {"identity": self.host_identity},
            "ledger": self.ledger.to_dict(),
            "secrets": {
                reference.name: reference.reference.to_string()
                for reference in self.secret_refs
            },
        }


def load_config(
    *,
    explicit: Mapping[str, object] | None = None,
    environment: Mapping[str, object] | None = None,
    config_path: str | Path | None = None,
    toml_data: Mapping[str, object] | None = None,
) -> AppConfig:
    """Load immutable configuration from explicitly supplied sources."""
    return AppConfig.from_sources(
        explicit=explicit,
        environment=environment,
        config_path=config_path,
        toml_data=toml_data,
    )
