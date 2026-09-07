from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from open_brain.local_data import (
    LocalDataError,
    LocalRootSelection,
    prepare_local_root,
    select_local_root,
)


def _private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def _filesystem(_path: Path, platform_name: str) -> str:
    return "apfs" if platform_name == "darwin" else "ext4"


def test_select_local_root_uses_exact_platform_defaults_and_override(tmp_path: Path) -> None:
    home = _private_directory(tmp_path / "home")

    macos = select_local_root(
        data_dir=None,
        environment={"HOME": str(home)},
        platform_name="darwin",
    )
    linux = select_local_root(
        data_dir=None,
        environment={"HOME": str(home)},
        platform_name="linux",
    )
    xdg = select_local_root(
        data_dir=None,
        environment={"HOME": str(home), "XDG_DATA_HOME": str(home / "xdg")},
        platform_name="linux",
    )
    override = select_local_root(
        data_dir=str(home / "chosen-brain"),
        environment={"HOME": str(home), "XDG_DATA_HOME": str(home / "ignored")},
        platform_name="linux",
    )

    assert macos.data_home == home / "Library/Application Support/open-brain"
    assert macos.brain_root == home / "Library/Application Support/open-brain/brain"
    assert linux.data_home == home / ".local/share/open-brain"
    assert linux.brain_root == home / ".local/share/open-brain/brain"
    assert xdg.data_home == home / "xdg/open-brain"
    assert xdg.brain_root == home / "xdg/open-brain/brain"
    assert override.data_home is None
    assert override.brain_root == home / "chosen-brain"


@pytest.mark.parametrize(
    ("platform_name", "relative"),
    (("win32", None), ("linux", "relative"), ("darwin", "../relative")),
)
def test_select_local_root_rejects_unsupported_or_relative_inputs(
    tmp_path: Path, platform_name: str, relative: str | None
) -> None:
    home = _private_directory(tmp_path / "home")

    with pytest.raises(LocalDataError):
        select_local_root(
            data_dir=relative,
            environment={"HOME": str(home)},
            platform_name=platform_name,
        )


@pytest.mark.parametrize(
    ("platform_name", "relative"),
    (
        ("darwin", "Library/Mobile Documents/open-brain"),
        ("darwin", "Library/CloudStorage/Provider/open-brain"),
        ("darwin", "Dropbox/open-brain"),
        ("darwin", "OneDrive/open-brain"),
        ("darwin", "Google Drive/open-brain"),
        ("linux", "Dropbox/open-brain"),
        ("linux", "OneDrive/open-brain"),
        ("linux", "Google Drive/open-brain"),
        ("linux", "Nextcloud/open-brain"),
        ("linux", "Syncthing/open-brain"),
        ("linux", "Sync/open-brain"),
    ),
)
def test_select_local_root_rejects_named_synchronized_locations(
    tmp_path: Path, platform_name: str, relative: str
) -> None:
    home = _private_directory(tmp_path / "home")

    with pytest.raises(LocalDataError, match="synchronized"):
        select_local_root(
            data_dir=str(home / relative),
            environment={"HOME": str(home)},
            platform_name=platform_name,
        )


def test_prepare_local_root_creates_only_private_managed_directories(tmp_path: Path) -> None:
    home = _private_directory(tmp_path / "home")
    selection = select_local_root(
        data_dir=None,
        environment={"HOME": str(home)},
        platform_name="darwin",
    )

    with prepare_local_root(selection, filesystem_type_probe=_filesystem) as prepared:
        assert prepared.filesystem_type == "apfs"
        assert prepared.root_identity == (
            selection.brain_root.stat().st_dev,
            selection.brain_root.stat().st_ino,
        )
        prepared.revalidate()

    assert stat.S_IMODE(selection.data_home.stat().st_mode) == 0o700  # type: ignore[union-attr]
    assert stat.S_IMODE(selection.brain_root.stat().st_mode) == 0o700


def test_prepare_local_root_rejects_ancestor_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    home = _private_directory(tmp_path / "home")
    outside = _private_directory(tmp_path / "outside")
    (home / "Library").symlink_to(outside, target_is_directory=True)
    selection = select_local_root(
        data_dir=None,
        environment={"HOME": str(home)},
        platform_name="darwin",
    )

    with pytest.raises(LocalDataError, match="unsafe component"), prepare_local_root(
        selection, filesystem_type_probe=_filesystem
    ):
        raise AssertionError("unreachable")

    assert list(outside.iterdir()) == []


def test_prepare_local_root_rejects_instead_of_repairing_permissive_managed_root(
    tmp_path: Path,
) -> None:
    home = _private_directory(tmp_path / "home")
    data_home = home / ".local/share/open-brain"
    data_home.mkdir(parents=True, mode=0o755)
    data_home.chmod(0o755)
    selection = select_local_root(
        data_dir=None,
        environment={"HOME": str(home)},
        platform_name="linux",
    )

    with pytest.raises(LocalDataError, match="permissions"), prepare_local_root(
        selection, filesystem_type_probe=_filesystem
    ):
        raise AssertionError("unreachable")

    assert stat.S_IMODE(data_home.stat().st_mode) == 0o755
    assert not selection.brain_root.exists()


def test_prepare_local_root_rejects_foreign_owner_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _private_directory(tmp_path / "home")
    selected = LocalRootSelection(
        home=home,
        data_home=None,
        brain_root=home,
        platform_name="linux",
    )
    effective_uid = os.geteuid()
    monkeypatch.setattr("open_brain.local_data.os.geteuid", lambda: effective_uid + 10_000)

    with pytest.raises(LocalDataError, match="owned"), prepare_local_root(
        selected, filesystem_type_probe=_filesystem
    ):
        raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("platform_name", "filesystem_type"),
    tuple(("darwin", item) for item in ("afpfs", "nfs", "smbfs", "webdav"))
    + tuple(
        ("linux", item)
        for item in ("9p", "afs", "ceph", "cifs", "davfs", "fuse.sshfs", "nfs", "nfs4", "smb3")
    ),
)
def test_prepare_local_root_rejects_named_network_filesystems_before_creation(
    tmp_path: Path, platform_name: str, filesystem_type: str
) -> None:
    home = _private_directory(tmp_path / "home")
    selection = select_local_root(
        data_dir=None,
        environment={"HOME": str(home)},
        platform_name=platform_name,
    )

    with pytest.raises(LocalDataError, match="network filesystem"), prepare_local_root(
        selection,
        filesystem_type_probe=lambda _path, _platform: filesystem_type,
    ):
        raise AssertionError("unreachable")

    assert selection.data_home is not None
    assert not selection.data_home.exists()


def test_prepared_root_revalidation_rejects_replacement_and_symlink_races(
    tmp_path: Path,
) -> None:
    home = _private_directory(tmp_path / "home")
    root = _private_directory(home / "brain")
    selection = select_local_root(
        data_dir=str(root),
        environment={"HOME": str(home)},
        platform_name="linux",
    )

    with prepare_local_root(selection, filesystem_type_probe=_filesystem) as prepared:
        pinned = home / "pinned"
        root.rename(pinned)
        _private_directory(root)
        with pytest.raises(LocalDataError, match="changed"):
            prepared.revalidate()
        root.rmdir()
        root.symlink_to(pinned, target_is_directory=True)
        with pytest.raises(LocalDataError, match="unsafe component"):
            prepared.revalidate()
