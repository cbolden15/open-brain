from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from open_brain_legacy.integrations.repository_identity import (
    RepositoryExcludedError,
    RepositoryIdentityError,
    RepositoryIdentityResolver,
    RepositoryIdentitySource,
    normalize_origin,
)


def _git(*args: str, cwd: Path) -> str:
    completed = subprocess.run(
        ("git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=Fixture", *args),
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return completed.stdout.strip()


def _make_repo(path: Path, *, origin: str | None = None) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", cwd=path)
    (path / "fixture.txt").write_text("synthetic\n", encoding="utf-8")
    _git("add", "fixture.txt", cwd=path)
    _git("commit", "-q", "-m", "synthetic fixture", cwd=path)
    if origin is not None:
        _git("remote", "add", "origin", origin, cwd=path)
    return path


@pytest.mark.parametrize(
    ("origin", "expected"),
    (
        ("https://github.example/acme/widget.git", "acme/widget"),
        ("git@github.example:acme/widget.git", "acme/widget"),
        ("ssh://git@github.example/acme/widget.git", "acme/widget"),
        (None, None),
        ("", None),
    ),
)
def test_normalize_origin(origin: str | None, expected: str | None) -> None:
    assert normalize_origin(origin) == expected


def test_identity_is_stable_for_nested_paths_and_worktrees(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    main = _make_repo(
        projects / "widget",
        origin="git@github.example:acme/widget.git",
    )
    nested = main / "src" / "package"
    nested.mkdir(parents=True)
    worktree = projects / "widget-worktree"
    _git("worktree", "add", "-q", "-b", "fixture-worktree", str(worktree), cwd=main)
    resolver = RepositoryIdentityResolver(projects_root=projects)

    identities = (resolver.identify(main), resolver.identify(nested), resolver.identify(worktree))

    assert identities[0] == identities[1] == identities[2]
    assert identities[0].slug == "acme/widget"
    assert identities[0].source is RepositoryIdentitySource.ORIGIN
    assert identities[0].repository_id.startswith("repo_")
    assert str(tmp_path) not in repr(identities[0].to_dict())
    assert set(identities[0].to_dict()) == {"repository_id", "slug", "source"}


def test_no_origin_uses_bounded_fallback_and_opaque_ids_avoid_collisions(
    tmp_path: Path,
) -> None:
    projects = tmp_path / "projects"
    nested = _make_repo(projects / "Group" / "local-tool")
    first_external = _make_repo(tmp_path / "external-a" / "same-name")
    second_external = _make_repo(tmp_path / "external-b" / "same-name")
    resolver = RepositoryIdentityResolver(projects_root=projects)

    nested_identity = resolver.identify(nested)
    first_identity = resolver.identify(first_external)
    second_identity = resolver.identify(second_external)

    assert nested_identity.slug == "Group/local-tool"
    assert nested_identity.source is RepositoryIdentitySource.PROJECT_RELATIVE
    assert first_identity.slug == second_identity.slug == "same-name"
    assert first_identity.source is second_identity.source is RepositoryIdentitySource.BASENAME
    assert first_identity.repository_id != second_identity.repository_id
    assert all(
        str(tmp_path) not in repr(value.to_dict())
        for value in (first_identity, second_identity)
    )


def test_same_slug_from_different_origin_hosts_has_distinct_identity(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    first = _make_repo(
        projects / "first",
        origin="https://github.example/acme/widget.git",
    )
    second = _make_repo(
        projects / "second",
        origin="https://gitlab.example/acme/widget.git",
    )
    resolver = RepositoryIdentityResolver(projects_root=projects)

    first_identity = resolver.identify(first)
    second_identity = resolver.identify(second)

    assert first_identity.slug == second_identity.slug == "acme/widget"
    assert first_identity.repository_id != second_identity.repository_id


def test_excluded_and_non_repository_paths_fail_with_structural_errors(tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    excluded = _make_repo(projects / "Clients" / "fixture")
    plain = projects / "plain"
    plain.mkdir(parents=True)
    resolver = RepositoryIdentityResolver(
        projects_root=projects,
        exclusions=("Clients/*",),
    )

    with pytest.raises(RepositoryExcludedError, match="repository excluded"):
        resolver.identify(excluded)
    with pytest.raises(RepositoryIdentityError, match="repository unavailable"):
        resolver.identify(plain)
