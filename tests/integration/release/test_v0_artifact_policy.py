from __future__ import annotations

import json
import tarfile
import tomllib
import zipfile
from io import BytesIO
from pathlib import Path
from typing import cast

from tools.open_brain_dev.artifact_policy import required_members_for_policy, verify_artifacts

ROOT = Path(__file__).parents[3]
POLICY_PATH = ROOT / "release" / "v0-artifact-policy.json"
MANIFEST_PATH = ROOT / "docs" / "v0-package-classification.json"
DOCUMENTATION_PATH = ROOT / "docs" / "artifact-characterization.md"
ARTIFACT_NAMES = {
    ("app", "wheel"): "open_brain-0.1.0-py3-none-any.whl",
    ("app", "sdist"): "open_brain-0.1.0.tar.gz",
    ("connector", "wheel"): "open_brain_connectors-0.1.0-py3-none-any.whl",
    ("connector", "sdist"): "open_brain_connectors-0.1.0.tar.gz",
    ("engine", "wheel"): "open_brain_engine-0.1.0-py3-none-any.whl",
    ("engine", "sdist"): "open_brain_engine-0.1.0.tar.gz",
}


def _policy() -> dict[str, object]:
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("artifact policy must be an object")
    return cast(dict[str, object], value)


def _distribution_config(distribution: str) -> dict[str, object]:
    distributions = _policy()["python_distributions"]
    assert isinstance(distributions, dict) and isinstance(distributions[distribution], dict)
    return cast(dict[str, object], distributions[distribution])


def _artifact_config(distribution: str, kind: str) -> dict[str, object]:
    artifacts = _distribution_config(distribution)["artifacts"]
    assert isinstance(artifacts, dict) and isinstance(artifacts[kind], dict)
    return cast(dict[str, object], artifacts[kind])


def _write_wheel(path: Path, members: list[str]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for member in members:
            archive.writestr(member, "synthetic")


def _write_sdist(path: Path, members: list[str]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for member in members:
            data = b"synthetic"
            info = tarfile.TarInfo(f"open_brain-0.1.0/{member}")
            info.size = len(data)
            archive.addfile(info, BytesIO(data))


def _write_policy_artifacts(tmp_path: Path) -> dict[tuple[str, str], Path]:
    artifacts: dict[tuple[str, str], Path] = {}
    for coordinate, name in ARTIFACT_NAMES.items():
        distribution, kind = coordinate
        path = tmp_path / name
        members = list(required_members_for_policy(POLICY_PATH, distribution, kind))
        if kind == "wheel":
            _write_wheel(path, members)
        else:
            _write_sdist(path, members)
        artifacts[coordinate] = path
    return artifacts


def _manifest_members(distribution: str, kind: str) -> set[str]:
    payload = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    records = [
        *payload["files"].values(),
        *payload["phase4"]["subjects"].values(),
    ]
    label = f"{distribution}-{kind}"
    distribution_config = _distribution_config(distribution)
    project_root = cast(str, distribution_config["project_root"])
    members: set[str] = set()
    for record in records:
        if label not in record["artifact_disposition"]:
            continue
        current = str(record["current_path"])
        if current.startswith(f"{project_root}/src/"):
            relative = current.removeprefix(f"{project_root}/")
            members.add(relative if kind == "sdist" else relative.removeprefix("src/"))
        elif current.startswith(f"{project_root}/"):
            members.add(current.removeprefix(f"{project_root}/"))
        else:
            members.add(current)
    rewrites_by_kind = cast(dict[str, object], distribution_config["member_rewrites"])
    rewrites = cast(dict[str, str], rewrites_by_kind[kind])
    return {rewrites.get(member, member.partition("#")[0]) for member in members}


def test_phase_four_policy_declares_all_python_artifact_coordinates() -> None:
    policy = _policy()
    distributions = policy["python_distributions"]

    assert policy["policy_version"] == 6
    assert policy["phase"] == "ob1-w0-base-package-bootstrap"
    assert isinstance(distributions, dict)
    assert set(distributions) == {"app", "connector", "engine"}
    for name in ("app", "connector", "engine"):
        distribution = distributions[name]
        assert isinstance(distribution, dict)
        assert set(distribution["artifacts"]) == {"sdist", "wheel"}
        assert set(distribution["artifact_name_patterns"]) == {"sdist", "wheel"}


def test_phase_four_policy_matches_each_explicit_hatch_configuration() -> None:
    assert _policy()["canonical_manifest"] == {
        "path": "docs/v0-package-classification.json"
    }
    for distribution, project_root, status in (
        ("app", "packages/app", "app-isolated-unpublished"),
        ("connector", "packages/connectors", "connector-isolated-unpublished"),
        ("engine", "packages/engine", "engine-isolated-unpublished"),
    ):
        pyproject = tomllib.loads(
            (ROOT / project_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        hatch_build = pyproject["tool"]["hatch"]["build"]
        build = hatch_build["targets"]
        wheel = _artifact_config(distribution, "wheel")
        sdist = _artifact_config(distribution, "sdist")
        config = _distribution_config(distribution)

        assert hatch_build["ignore-vcs"] is True
        assert hatch_build["hooks"]["custom"] == {"path": "hatch_build.py"}
        assert config["build_hook"] == f"{project_root}/hatch_build.py"
        assert config["project_root"] == project_root
        assert build["wheel"]["packages"] == wheel["configured_package_roots"]
        assert build["wheel"].get("force-include", {}) == wheel["force_includes"]
        assert build["sdist"]["include"] == sdist["configured_includes"]
        assert build["sdist"]["exclude"] == sdist["configured_exclusions"]
        assert build["sdist"]["force-include"] == sdist["force_includes"]
        assert wheel["status"] == sdist["status"] == status
        assert (ROOT / project_root / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
        assert (ROOT / project_root / "NOTICE").read_bytes() == (ROOT / "NOTICE").read_bytes()


def test_supported_v0_python_runtime_is_314() -> None:
    root_project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    compatibility = json.loads(
        (ROOT / "release/phase4-compatibility.json").read_text(encoding="utf-8")
    )

    assert root_project["tool"]["ruff"]["target-version"] == "py314"
    assert root_project["tool"]["mypy"]["python_version"] == "3.14"
    assert compatibility["python"]["source_and_wheel"] == ["3.14"]
    for project_root in ("packages/app", "packages/connectors", "packages/engine"):
        project = tomllib.loads(
            (ROOT / project_root / "pyproject.toml").read_text(encoding="utf-8")
        )
        python_classifiers = [
            value
            for value in project["project"]["classifiers"]
            if value.startswith("Programming Language :: Python :: 3.")
        ]
        assert python_classifiers == ["Programming Language :: Python :: 3.14"]


def test_phase_four_policy_verifies_all_distributions_and_rejects_leaks(
    tmp_path: Path,
) -> None:
    artifacts = _write_policy_artifacts(tmp_path)
    paths = list(artifacts.values())

    assert verify_artifacts(POLICY_PATH, paths) == []

    leaked = "open_brain_engine/portable/conformance/v1/.open-brain/x"
    engine_wheel = artifacts[("engine", "wheel")]
    engine_members = list(required_members_for_policy(POLICY_PATH, "engine", "wheel"))
    _write_wheel(engine_wheel, [*engine_members, leaked])
    findings = verify_artifacts(POLICY_PATH, paths)
    assert [(finding.member, finding.rule) for finding in findings] == [
        (leaked, "forbidden-member")
    ]

    undeclared = "unclassified.txt"
    _write_wheel(engine_wheel, [*engine_members, undeclared])
    findings = verify_artifacts(POLICY_PATH, paths)
    assert [(finding.member, finding.rule) for finding in findings] == [
        (undeclared, "undeclared-member")
    ]


def test_phase_four_policy_keys_duplicates_and_missing_artifacts_by_distribution(
    tmp_path: Path,
) -> None:
    artifacts = _write_policy_artifacts(tmp_path)
    duplicate = tmp_path / "open_brain-duplicate-py3-none-any.whl"
    app_members = list(required_members_for_policy(POLICY_PATH, "app", "wheel"))
    _write_wheel(duplicate, app_members)

    findings = verify_artifacts(POLICY_PATH, [*artifacts.values(), duplicate])
    assert [(finding.artifact, finding.member, finding.rule) for finding in findings] == [
        (duplicate.name, "<artifact>", "duplicate-artifact-coordinate")
    ]

    missing_app_sdist = [
        path for coordinate, path in artifacts.items() if coordinate != ("app", "sdist")
    ]
    findings = verify_artifacts(POLICY_PATH, missing_app_sdist)
    assert [(finding.artifact, finding.member, finding.rule) for finding in findings] == [
        ("<artifacts>", "app-sdist", "missing-artifact-coordinate")
    ]


def test_phase_four_build_and_ci_verify_all_python_artifacts() -> None:
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "uv build --no-sources --project packages/engine --out-dir dist" in makefile
    assert "uv build --no-sources --project packages/app --out-dir dist" in makefile
    assert "uv build --no-sources --project packages/connectors --out-dir dist" in makefile
    assert "rm -rf dist" not in makefile
    assert "- run: make verify-artifacts" in workflow
    assert "packages/app/tests/integration/services/test_appliance_upgrade.py" in workflow
    assert "packages/app/tests/integration/services/test_appliance_uninstall.py" in workflow
    assert "packages/app/tests/integration/services/test_appliance_supervisors.py" in workflow


def test_phase_four_policy_derives_exact_membership_from_canonical_manifest() -> None:
    for distribution in ("app", "connector", "engine"):
        for kind in ("wheel", "sdist"):
            assert set(
                required_members_for_policy(POLICY_PATH, distribution, kind)
            ) == _manifest_members(distribution, kind)


def test_phase_zero_policy_requires_every_schema_and_conformance_fixture() -> None:
    wheel_members = set(required_members_for_policy(POLICY_PATH, "engine", "wheel"))
    sdist_members = set(required_members_for_policy(POLICY_PATH, "engine", "sdist"))
    schema_files = {
        path.relative_to(
            ROOT / "packages/engine/src/open_brain_engine/portable/schemas/v1"
        ).as_posix()
        for path in (ROOT / "packages/engine/src/open_brain_engine/portable/schemas/v1").rglob(
            "*"
        )
        if path.is_file()
    }
    fixture_files = {
        path.relative_to(
            ROOT / "packages/engine/src/open_brain_engine/portable/conformance/v1"
        ).as_posix()
        for path in (
            ROOT / "packages/engine/src/open_brain_engine/portable/conformance/v1"
        ).rglob("*")
        if path.is_file()
    }

    assert {
        f"open_brain_engine/portable/schemas/v1/{path}" for path in schema_files
    } == {
        path
        for path in wheel_members
        if path.startswith("open_brain_engine/portable/schemas/v1/")
    }
    assert {
        f"open_brain_engine/portable/conformance/v1/{path}" for path in fixture_files
    } == {
        path
        for path in wheel_members
        if path.startswith("open_brain_engine/portable/conformance/v1/")
    }
    assert {
        f"src/open_brain_engine/portable/schemas/v1/{path}" for path in schema_files
    } <= sdist_members
    assert {
        f"src/open_brain_engine/portable/conformance/v1/{path}" for path in fixture_files
    } <= sdist_members


def test_shared_portability_schema_and_vectors_are_required_in_engine_artifacts() -> None:
    wheel_members = set(required_members_for_policy(POLICY_PATH, "engine", "wheel"))
    sdist_members = set(required_members_for_policy(POLICY_PATH, "engine", "sdist"))
    relative_members = {
        path.relative_to(
            ROOT / "packages/engine/src/open_brain_engine/portability"
        ).as_posix()
        for path in (ROOT / "packages/engine/src/open_brain_engine/portability").rglob("*")
        if path.is_file() and path.suffix == ".json"
    }

    assert {
        f"open_brain_engine/portability/{path}" for path in relative_members
    } <= wheel_members
    assert {
        f"src/open_brain_engine/portability/{path}" for path in relative_members
    } <= sdist_members


def test_target_boundary_names_legacy_optional_connector_and_cloud_exclusions() -> None:
    exclusions = _policy()["target_release_exclusions"]
    assert isinstance(exclusions, list)
    assert {
        "open_brain/dev/**",
        "open_brain/migrate/**",
        "open_brain/parity/**",
        "open_brain/providers/optional_cloud.py",
        "open_brain/production/media.py",
        "open_brain/production/youtube_bridge.py",
        "open_brain/production/project_commit_bridge.py",
        "open_brain/capture/extractors/social.py",
        "open_brain/capture/extractors/youtube.py",
        "open_brain/integrations/messaging.py",
        "open_brain/integrations/obsidian.py",
        "open_brain/operations/optional_jobs.py",
    } <= set(exclusions)


def test_phase_zero_artifact_policy_has_exact_supported_and_unsupported_hosts() -> None:
    policy = _policy()
    hosts = policy["hosts"]
    assert isinstance(hosts, dict)
    assert hosts["supported"] == [
        {
            "artifact": "python-source-or-wheel",
            "architecture": "arm64",
            "operating_system": "macOS",
            "versions": ">=14",
        },
        {
            "artifact": "linux-x86_64",
            "architecture": "x86_64",
            "operating_system": "Ubuntu",
            "versions": ["24.04 LTS", "26.04 LTS"],
        },
        {
            "artifact": "linux-x86_64",
            "architecture": "x86_64",
            "operating_system": "Debian",
            "versions": ["13"],
        },
    ]
    assert hosts["unsupported"] == ["macos-x86_64", "linux-arm64", "windows"]
    assert policy["publication_scope"] == {
        "version": "0.1.0",
        "status": "unpublished",
        "installations": {
            "macos-arm64": {
                "checksum": "sha256",
                "format": "tar.gz",
                "installer": "install.sh",
                "method": "native-archive",
                "python": "3.14-bundled",
            },
            "linux-x86_64": {
                "checksum": "sha256",
                "format": "tar.gz",
                "installer": "install.sh",
                "method": "native-archive",
                "python": "3.14-bundled",
            },
        },
    }
    assert policy["base_native_artifacts"] == {
        "bundler": "PyInstaller 6.22.2 onedir",
        "entrypoint": "open_brain.services.local_native_entrypoint",
        "forbidden_dependency_roots": [
            "argon2",
            "cryptography",
            "keyring",
            "sqlcipher3",
            "starlette",
            "uvicorn",
        ],
        "forbidden_product_roots": [
            "open_brain.capture",
            "open_brain.cli",
            "open_brain.extensions",
            "open_brain.integrations",
            "open_brain.services.appliance_*",
            "open_brain.services.http_server",
            "open_brain.services.mcp_stdio",
            "open_brain.services.native_artifacts",
            "open_brain.services.native_entrypoint",
            "open_brain.services.secure_node_entrypoints",
            "open_brain_connectors",
            "open_brain_engine.portability.secure_node",
            "open_brain_engine.protocol.custody",
            "open_brain_legacy",
        ],
        "install_layout": {
            "launcher": "$HOME/.local/bin/open-brain",
            "payload": "$HOME/.local/lib/open-brain",
        },
        "installer": "release/open-brain/install.sh",
        "manifest": "open-brain-release-manifest-v1.txt",
        "published": [],
        "spec": "release/open-brain/open-brain.spec",
        "status": "ob1-w0-local-build-verified-unpublished",
        "supported_platforms": ["linux-x86_64", "macos-arm64"],
    }
    assert policy["native_artifacts"] == {
        "bundler_candidate": "PyInstaller 6 onedir",
        "fallback": "Nuitka standalone",
        "member_policy": {
            "allowed_exact": [
                "_internal",
                "_internal/Python",
                "_internal/base_library.zip",
                "open-brain",
            ],
            "allowed_internal_library_pattern": (
                r"^_internal/lib[A-Za-z0-9._+-]+(?:\.dylib|\.so(?:\.[0-9]+)*)$"
            ),
            "allowed_resource_roots": [
                "_internal/open_brain/resources/supervisors",
                "_internal/open_brain_engine/portable",
            ],
            "allowed_trees": [
                "_internal/Python.framework",
                "_internal/python3.12",
            ],
            "forbidden_components": [
                ".env",
                ".git",
                ".github",
                "__pycache__",
                "credential",
                "credentials",
                "open_brain_connectors",
                "open_brain_legacy",
                "packages",
                "private",
                "secret",
                "secrets",
                "state",
                "tests",
                "tools",
            ],
            "forbidden_suffixes": [
                ".credential",
                ".credentials",
                ".db",
                ".py",
                ".pyc",
                ".pyo",
                ".sqlite",
                ".sqlite3",
                ".token",
            ],
        },
        "profile": "secure-node-precursor",
        "published": [],
        "status": "p4w6-unpublished-release-candidate",
    }


def test_artifact_characterization_distinguishes_python_and_native_status() -> None:
    document = DOCUMENTATION_PATH.read_text(encoding="utf-8")

    assert "engine-isolated-unpublished" in document
    assert "app-isolated-unpublished" in document
    assert "Native build subjects are present" in document
    assert "does not" in document
    assert "does not claim that a native v0 release exists" in document
