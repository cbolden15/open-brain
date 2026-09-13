import logging
from pathlib import Path
from typing import cast

import pytest
from open_brain_engine.core.models import PrivacyTier

from open_brain.config import (
    AppConfig,
    ConfigError,
    LedgerRouteConfig,
    LedgerTaxonomyConfig,
    NamedSecretRef,
    RetainedRootIdentities,
    RetainedRoots,
    SecretRef,
    SecretRefKind,
    SecretResolutionError,
    resolve_secret,
)


def _retained_roots(base: Path) -> RetainedRoots:
    return RetainedRoots(
        work=base / "work",
        personal=base / "personal",
        capture=base / "capture",
        saved_content=base / "saved-content",
        state=base / "state",
    )


def _explicit_roots(base: Path) -> dict[str, object]:
    return {
        "paths": {
            **_retained_roots(base).to_dict(),
            "backup_root": str(base / "backup"),
        }
    }


def test_retained_roots_are_absolute_distinct_and_preserved(tmp_path: Path) -> None:
    roots = RetainedRoots(
        work=tmp_path / "work",
        personal=tmp_path / "personal",
        capture=tmp_path / "capture",
        saved_content=tmp_path / "saved-content",
        state=tmp_path / "state",
    )

    assert roots.to_dict() == {
        "work_root": str(tmp_path / "work"),
        "personal_root": str(tmp_path / "personal"),
        "capture_root": str(tmp_path / "capture"),
        "saved_content_root": str(tmp_path / "saved-content"),
        "state_root": str(tmp_path / "state"),
    }
    assert all(not path.exists() for path in roots.as_tuple())

    with pytest.raises(ConfigError, match="distinct"):
        RetainedRoots(
            work=tmp_path / "same",
            personal=tmp_path / "same",
            capture=tmp_path / "capture-2",
            saved_content=tmp_path / "saved-content-2",
            state=tmp_path / "state-2",
        )

    with pytest.raises(ConfigError, match="absolute"):
        RetainedRoots(
            work=Path("relative"),
            personal=tmp_path / "personal-3",
            capture=tmp_path / "capture-3",
            saved_content=tmp_path / "saved-content-3",
            state=tmp_path / "state-3",
        )


def test_app_config_loads_all_retained_roots_without_relocation(tmp_path: Path) -> None:
    expected = RetainedRoots(
        work=tmp_path / "work",
        personal=tmp_path / "personal",
        capture=tmp_path / "capture",
        saved_content=tmp_path / "saved-content",
        state=tmp_path / "state",
    )

    config = AppConfig.from_sources(explicit=_explicit_roots(tmp_path), environment={})

    assert config.roots == expected
    assert config.work_root == expected.work
    assert config.personal_root == expected.personal
    assert config.capture_root == expected.capture
    assert config.saved_content_root == expected.saved_content
    assert config.state_root == expected.state
    assert config.backup_root == tmp_path / "backup"


def test_retained_roots_reject_ancestor_descendant_overlap(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="non-overlapping"):
        RetainedRoots(
            work=tmp_path / "work",
            personal=tmp_path / "work" / "personal",
            capture=tmp_path / "capture",
            saved_content=tmp_path / "saved-content",
            state=tmp_path / "state",
        )

    with pytest.raises(ConfigError, match="backup root"):
        AppConfig(roots=_retained_roots(tmp_path), backup=tmp_path / "work")


def test_retained_roots_reject_injected_physical_aliases(tmp_path: Path) -> None:
    roots = _retained_roots(tmp_path)
    identities = RetainedRootIdentities(
        work="physical-work",
        personal="physical-work",
        capture="physical-capture",
        saved_content="physical-saved",
        state="physical-state",
    )

    with pytest.raises(ConfigError, match="identities must be distinct"):
        roots.validate_identities(identities)


def test_secret_ref_parses_only_typed_environment_and_file_references(tmp_path: Path) -> None:
    environment_ref = SecretRef.parse("env:SYNTHETIC_TOKEN")
    file_ref = SecretRef.parse(f"file:{tmp_path / 'credential'}")

    assert environment_ref == SecretRef(SecretRefKind.ENVIRONMENT, "SYNTHETIC_TOKEN")
    assert file_ref == SecretRef(SecretRefKind.FILE, str(tmp_path / "credential"))


@pytest.mark.parametrize(
    "raw_reference",
    [
        "",
        "ENV:SYNTHETIC_TOKEN",
        "env:",
        "env:1INVALID",
        "env:INVALID-NAME",
        "file:",
        "file:relative/credential",
        "file:~/credential",
        "file://synthetic/credential",
        f"file:/synthetic/{chr(10)}credential",
        "vault:synthetic",
    ],
)
def test_secret_ref_rejects_malformed_or_unsupported_references(raw_reference: str) -> None:
    with pytest.raises(ConfigError):
        SecretRef.parse(raw_reference)


def test_config_loads_only_named_typed_secret_references(tmp_path: Path) -> None:
    config = AppConfig.from_sources(
        explicit=_explicit_roots(tmp_path),
        toml_data={
            "secrets": {
                "provider_token": "env:SYNTHETIC_PROVIDER_TOKEN",
                "service_token": "file:/synthetic/service-token",
            }
        },
    )

    assert config.secret_refs == (
        NamedSecretRef("provider_token", SecretRef.parse("env:SYNTHETIC_PROVIDER_TOKEN")),
        NamedSecretRef("service_token", SecretRef.parse("file:/synthetic/service-token")),
    )
    assert config.to_dict()["secrets"] == {
        "provider_token": "env:SYNTHETIC_PROVIDER_TOKEN",
        "service_token": "file:/synthetic/service-token",
    }

    with pytest.raises(ConfigError, match="secret reference"):
        AppConfig.from_sources(
            explicit=_explicit_roots(tmp_path),
            toml_data={"secrets": {"provider_token": "synthetic-secret-value"}},
        )


def test_secret_resolver_returns_only_supplied_environment_value(tmp_path: Path) -> None:
    canary = "synthetic" + "-resolved-" + "value"
    file_reads: list[Path] = []

    def unexpected_file_read(path: Path) -> str:
        file_reads.append(path)
        return "unreachable"

    resolved = resolve_secret(
        SecretRef.parse("env:SYNTHETIC_TOKEN"),
        environment={"SYNTHETIC_TOKEN": canary},
        file_reader=unexpected_file_read,
    )

    assert resolved == canary
    assert file_reads == []


def test_secret_resolver_returns_only_injected_file_value(tmp_path: Path) -> None:
    canary = "synthetic" + "-file-" + "value"
    credential_path = tmp_path / "credential"
    file_reads: list[Path] = []

    def read_secret(path: Path) -> str:
        file_reads.append(path)
        return canary

    resolved = resolve_secret(
        SecretRef.parse(f"file:{credential_path}"),
        environment={},
        file_reader=read_secret,
    )

    assert resolved == canary
    assert file_reads == [credential_path]


def test_secret_resolver_does_not_read_ambient_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    canary = "synthetic" + "-ambient-" + "value"
    monkeypatch.setenv("SYNTHETIC_TOKEN", canary)

    with pytest.raises(SecretResolutionError, match="unavailable") as error:
        resolve_secret(
            SecretRef.parse("env:SYNTHETIC_TOKEN"),
            environment={},
            file_reader=lambda path: "unreachable",
        )

    assert canary not in str(error.value)


@pytest.mark.parametrize("resolved_value", ["", None])
def test_secret_resolver_rejects_empty_or_untyped_values(
    resolved_value: str | None,
) -> None:
    environment = cast(dict[str, str], {"SYNTHETIC_TOKEN": resolved_value})

    with pytest.raises(SecretResolutionError, match="unavailable"):
        resolve_secret(
            SecretRef.parse("env:SYNTHETIC_TOKEN"),
            environment=environment,
            file_reader=lambda path: "unreachable",
        )


def test_secret_file_errors_are_bounded_and_redacted(tmp_path: Path) -> None:
    canary = "synthetic" + "-reader-" + "failure"

    def failing_reader(path: Path) -> str:
        raise OSError(canary)

    with pytest.raises(SecretResolutionError) as error:
        resolve_secret(
            SecretRef.parse(f"file:{tmp_path / 'credential'}"),
            environment={},
            file_reader=failing_reader,
        )

    assert str(error.value) == "secret file value unavailable"
    assert error.value.__cause__ is None
    assert canary not in str(error.value)


def test_secret_resolver_rejects_empty_file_value(tmp_path: Path) -> None:
    with pytest.raises(SecretResolutionError, match="file value unavailable"):
        resolve_secret(
            SecretRef.parse(f"file:{tmp_path / 'credential'}"),
            environment={},
            file_reader=lambda path: "",
        )


def test_resolved_secret_never_enters_config_serialization_repr_or_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    canary = "synthetic" + "-nonpersistent-" + "value"
    resolved = resolve_secret(
        SecretRef.parse("env:SYNTHETIC_TOKEN"),
        environment={"SYNTHETIC_TOKEN": canary},
        file_reader=lambda path: "unreachable",
    )
    config = AppConfig.from_sources(explicit=_explicit_roots(tmp_path), environment={})

    logging.getLogger("synthetic.config").warning("config=%r", config)

    assert resolved == canary
    assert canary not in repr(config)
    assert canary not in repr(config.to_dict())
    assert canary not in caplog.text


def test_config_precedence_is_explicit_then_environment_then_toml_then_default(
    tmp_path: Path,
) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        """
[paths]
state_root = "/toml/state"
work_root = "/toml/work"
personal_root = "/toml/personal"
capture_root = "/toml/capture"
saved_content_root = "/toml/saved"
backup_root = "/toml/backup"

[providers]
default = "toml-provider"
cloud_enabled = true

[egress]
enabled = true
""".strip()
        + "\n",
        encoding="utf-8",
    )

    config = AppConfig.from_sources(
        explicit={"paths": {"capture_root": "/explicit/capture"}},
        environment={
            "OPEN_BRAIN_STATE_ROOT": "/environment/state",
            "OPEN_BRAIN_WORK_ROOT": "/environment/work",
            "OPEN_BRAIN_PERSONAL_ROOT": "/environment/personal",
            "OPEN_BRAIN_SAVED_CONTENT_ROOT": "/environment/saved",
            "OPEN_BRAIN_BACKUP_ROOT": "/environment/backup",
            "OPEN_BRAIN_PROVIDER": "environment-provider",
            "OPEN_BRAIN_CLOUD_ENABLED": "false",
        },
        config_path=toml_path,
    )

    assert config.state_root == Path("/environment/state")
    assert config.work_root == Path("/environment/work")
    assert config.capture_root == Path("/explicit/capture")
    assert config.saved_content_root == Path("/environment/saved")
    assert config.backup_root == Path("/environment/backup")
    assert config.provider == "environment-provider"
    assert config.cloud_enabled is False
    assert config.egress_enabled is True


def test_config_precedence_falls_through_per_field(tmp_path: Path) -> None:
    toml_path = tmp_path / "config.toml"
    toml_path.write_text(
        """
[paths]
state_root = "/toml/state"
work_root = "/toml/work"
personal_root = "/toml/personal"
capture_root = "/toml/capture"
saved_content_root = "/toml/saved"
backup_root = "/toml/backup"

[providers]
default = "toml-provider"
cloud_enabled = false

[egress]
enabled = false
""".strip()
        + "\n",
        encoding="utf-8",
    )
    explicit = {
        "state_root": "/explicit/state",
        "work_root": "/explicit/work",
        "personal_root": "/explicit/personal",
        "capture_root": "/explicit/capture",
        "saved_content_root": "/explicit/saved",
        "backup_root": "/explicit/backup",
        "provider": "explicit-provider",
        "cloud_enabled": True,
        "egress_enabled": True,
    }
    environment = {
        "OPEN_BRAIN_STATE_ROOT": "/environment/state",
        "OPEN_BRAIN_WORK_ROOT": "/environment/work",
        "OPEN_BRAIN_PERSONAL_ROOT": "/environment/personal",
        "OPEN_BRAIN_CAPTURE_ROOT": "/environment/capture",
        "OPEN_BRAIN_SAVED_CONTENT_ROOT": "/environment/saved",
        "OPEN_BRAIN_BACKUP_ROOT": "/environment/backup",
        "OPEN_BRAIN_PROVIDER": "environment-provider",
        "OPEN_BRAIN_CLOUD_ENABLED": "false",
        "OPEN_BRAIN_EGRESS_ENABLED": "false",
    }

    full = AppConfig.from_sources(explicit=explicit, environment=environment, config_path=toml_path)
    assert full.to_dict() == {
        "paths": {
            "work_root": "/explicit/work",
            "personal_root": "/explicit/personal",
            "capture_root": "/explicit/capture",
            "saved_content_root": "/explicit/saved",
            "state_root": "/explicit/state",
            "backup_root": "/explicit/backup",
        },
        "providers": {"default": "explicit-provider", "cloud_enabled": True},
        "egress": {"enabled": True},
        "host": {"identity": None},
        "ledger": {"taxonomy": {"version": "ledger-v1", "routes": []}},
        "secrets": {},
    }

    without_explicit = AppConfig.from_sources(environment=environment, config_path=toml_path)
    assert without_explicit.state_root == Path("/environment/state")
    assert without_explicit.provider == "environment-provider"
    assert without_explicit.cloud_enabled is False

    without_environment = AppConfig.from_sources(config_path=toml_path)
    assert without_environment.state_root == Path("/toml/state")
    assert without_environment.provider == "toml-provider"
    assert without_environment.cloud_enabled is False
    assert without_environment.egress_enabled is False


def test_safe_defaults_are_local_and_do_not_create_directories(tmp_path: Path) -> None:
    roots = _retained_roots(tmp_path)

    config = AppConfig.from_sources(explicit=_explicit_roots(tmp_path), environment={})

    assert config.provider == "local"
    assert config.cloud_enabled is False
    assert config.egress_enabled is False
    assert all(not root.exists() for root in roots.as_tuple())
    assert not config.backup_root.exists()


def test_host_identity_loads_from_explicit_environment_and_toml_sources(
    tmp_path: Path,
) -> None:
    explicit = AppConfig.from_sources(
        explicit={**_explicit_roots(tmp_path), "host": {"identity": "explicit-host"}},
        environment={"OPEN_BRAIN_HOST_IDENTITY": "environment-host"},
        toml_data={"host": {"identity": "toml-host"}},
    )
    environment = AppConfig.from_sources(
        explicit=_explicit_roots(tmp_path),
        environment={"OPEN_BRAIN_HOST_IDENTITY": "environment-host"},
        toml_data={"host": {"identity": "toml-host"}},
    )
    toml = AppConfig.from_sources(
        explicit=_explicit_roots(tmp_path),
        environment={},
        toml_data={"host": {"identity": "toml-host"}},
    )

    assert explicit.host_identity == "explicit-host"
    assert environment.host_identity == "environment-host"
    assert toml.host_identity == "toml-host"
    assert explicit.to_dict()["host"] == {"identity": "explicit-host"}


def test_host_identity_defaults_to_none(tmp_path: Path) -> None:
    config = AppConfig.from_sources(explicit=_explicit_roots(tmp_path), environment={})

    assert config.host_identity is None
    assert config.to_dict()["host"] == {"identity": None}


@pytest.mark.parametrize(
    "identity",
    ["", "Mac-mini", "mac_mini", "mac.mini", "1-mac-mini", "mac mini", "a" * 65],
)
def test_host_identity_rejects_values_outside_the_closed_identity_shape(
    identity: str,
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigError, match="host identity"):
        AppConfig.from_sources(
            explicit={**_explicit_roots(tmp_path), "host": {"identity": identity}},
            environment={},
        )


def test_config_does_not_read_ambient_environment_or_machine_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPEN_BRAIN_STATE_ROOT", "/ambient/state")
    monkeypatch.setenv("OPEN_BRAIN_WORK_ROOT", "/ambient/work")
    monkeypatch.setenv("OPEN_BRAIN_PERSONAL_ROOT", "/ambient/personal")
    monkeypatch.setenv("OPEN_BRAIN_CAPTURE_ROOT", "/ambient/capture")
    monkeypatch.setenv("OPEN_BRAIN_SAVED_CONTENT_ROOT", "/ambient/saved")
    monkeypatch.setenv("OPEN_BRAIN_BACKUP_ROOT", "/ambient/backup")
    monkeypatch.setenv("OPEN_BRAIN_PROVIDER", "ambient-provider")
    monkeypatch.setattr(Path, "home", lambda: (_ for _ in ()).throw(AssertionError("home read")))
    monkeypatch.setattr(Path, "cwd", lambda: (_ for _ in ()).throw(AssertionError("cwd read")))

    config = AppConfig.from_sources(explicit=_explicit_roots(tmp_path))

    assert config.provider == "local"
    assert config.state_root == tmp_path / "state"


@pytest.mark.parametrize(
    "toml_text",
    [
        "[unknown]\nvalue = true\n",
        '[paths]\nunknown = "/synthetic/path"\n',
        '[providers]\ncredential = "placeholder"\n',
        '[egress]\nenabled = "true"\n',
    ],
)
def test_config_rejects_unknown_toml_keys_and_malformed_values(
    tmp_path: Path, toml_text: str
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(toml_text, encoding="utf-8")

    with pytest.raises(ConfigError):
        AppConfig.from_sources(
            explicit=_explicit_roots(tmp_path),
            config_path=config_path,
        )


def test_config_rejects_malformed_environment_boolean(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cloud_enabled"):
        AppConfig.from_sources(
            explicit=_explicit_roots(tmp_path),
            environment={"OPEN_BRAIN_CLOUD_ENABLED": "maybe"},
        )


@pytest.mark.parametrize(
    "work_root",
    [
        "relative/work",
        "~/synthetic/work",
    ],
)
def test_config_rejects_unsafe_roots(work_root: str, tmp_path: Path) -> None:
    explicit = _explicit_roots(tmp_path)
    paths = cast(dict[str, object], explicit["paths"])
    paths["work_root"] = work_root

    with pytest.raises(ConfigError):
        AppConfig.from_sources(explicit=explicit, environment={})


def test_config_rejects_roots_that_normalize_to_the_same_path(tmp_path: Path) -> None:
    explicit = _explicit_roots(tmp_path)
    paths = cast(dict[str, object], explicit["paths"])
    paths["personal_root"] = tmp_path / "nested" / ".." / "work"

    with pytest.raises(ConfigError, match="distinct"):
        AppConfig.from_sources(explicit=explicit, environment={})


def test_config_is_immutable_and_contains_no_secret_values(tmp_path: Path) -> None:
    config = AppConfig.from_sources(explicit=_explicit_roots(tmp_path))

    with pytest.raises(AttributeError):
        config.provider = "changed"  # type: ignore[misc]
    assert set(config.to_dict()) == {
        "paths",
        "providers",
        "egress",
        "host",
        "ledger",
        "secrets",
    }
    assert config.to_dict()["secrets"] == {}
    assert "credential" not in repr(config).lower()


def test_ledger_taxonomy_defaults_to_an_immutable_empty_versioned_config(tmp_path: Path) -> None:
    config = AppConfig.from_sources(explicit=_explicit_roots(tmp_path))

    assert isinstance(config.ledger.taxonomy, LedgerTaxonomyConfig)
    assert config.ledger.taxonomy.version == "ledger-v1"
    assert config.ledger.taxonomy.routes == ()


def test_ledger_route_configuration_is_owned_by_the_application() -> None:
    route = LedgerRouteConfig.create(
        path_prefix=("professional",),
        topic_id="research",
        topic_label="Research",
        privacy_tier="work",
    )

    assert route.__class__.__module__ == "open_brain.config"
    assert route.privacy_tier == "work"


def test_ledger_taxonomy_loads_only_synthetic_relative_routes(tmp_path: Path) -> None:
    config = AppConfig.from_sources(
        explicit=_explicit_roots(tmp_path),
        toml_data={
            "ledger": {
                "taxonomy": {
                    "version": "synthetic-v1",
                    "routes": [
                        {
                            "path_prefix": ["professional", "research"],
                            "topic_id": "research",
                            "topic_label": "Research",
                            "privacy_tier": "work",
                        }
                    ],
                }
            }
        },
    )

    route = config.ledger.taxonomy.routes[0]
    assert route.path_prefix == ("professional", "research")
    assert route.privacy_tier == PrivacyTier.WORK.value


@pytest.mark.parametrize(
    "taxonomy",
    [
        {},
        {
            "version": "synthetic-v1",
            "routes": [
                {
                    "path_prefix": [".."],
                    "topic_id": "bad",
                    "topic_label": "Bad",
                    "privacy_tier": "work",
                }
            ],
        },
        {
            "version": "synthetic-v1",
            "routes": [
                {
                    "path_prefix": ["same"],
                    "topic_id": "one",
                    "topic_label": "One",
                    "privacy_tier": "work",
                },
                {
                    "path_prefix": ["same"],
                    "topic_id": "two",
                    "topic_label": "Two",
                    "privacy_tier": "work",
                },
            ],
        },
        {
            "version": "synthetic-v1",
            "routes": [
                {
                    "path_prefix": ["safe"],
                    "topic_id": "bad label",
                    "topic_label": "Bad",
                    "privacy_tier": "unknown",
                }
            ],
        },
        {
            "version": "synthetic-v1",
            "routes": [
                {
                    "path_prefix": ["safe"],
                    "topic_id": "research",
                    "topic_label": "# injected heading",
                    "privacy_tier": "work",
                }
            ],
        },
        {
            "version": "synthetic-v1",
            "routes": [
                {
                    "path_prefix": ["safe"],
                    "topic_id": "research",
                    "topic_label": "[injected](target)",
                    "privacy_tier": "work",
                }
            ],
        },
        {
            "version": "synthetic-v1",
            "routes": [
                {
                    "path_prefix": ["safe"],
                    "topic_id": "research",
                    "topic_label": "<synthetic-tag>",
                    "privacy_tier": "work",
                }
            ],
        },
    ],
)
def test_ledger_taxonomy_fails_closed_for_missing_or_unsafe_routes(
    tmp_path: Path, taxonomy: dict[str, object]
) -> None:
    config = AppConfig.from_sources(
        explicit=_explicit_roots(tmp_path),
        toml_data={"ledger": {"taxonomy": taxonomy}},
    )

    assert config.ledger.taxonomy.routes == ()


@pytest.mark.parametrize(
    "unsafe_character",
    ("\u0085", "\u2028", "\u2029", "\u200b", "\ud800"),
)
def test_ledger_taxonomy_fails_closed_for_unicode_structural_labels(
    tmp_path: Path, unsafe_character: str
) -> None:
    config = AppConfig.from_sources(
        explicit=_explicit_roots(tmp_path),
        toml_data={
            "ledger": {
                "taxonomy": {
                    "version": "synthetic-v1",
                    "routes": [
                        {
                            "path_prefix": ["safe"],
                            "topic_id": "research",
                            "topic_label": f"Safe{unsafe_character}forged",
                            "privacy_tier": "work",
                        }
                    ],
                }
            }
        },
    )

    assert config.ledger.taxonomy.routes == ()
