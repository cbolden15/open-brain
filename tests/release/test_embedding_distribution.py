"""Embedding resource in the base build, native audit and the v2 Homebrew formula.

Every archive and manifest here is synthetic; no real assets are needed.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

import tools.open_brain_dev.base_native as base_native
from open_brain.services import embedding_helper as eh
from tools.open_brain_dev import component_manifest_v2 as v2
from tools.open_brain_dev import release_v2
from tools.open_brain_dev.base_native import BaseNativeError

ROOT = Path(__file__).parents[2]
DIGEST = "1" * 64


def test_embedding_modules_are_required_in_the_base() -> None:
    assert {
        "open_brain.services.component_manifest_v2",
        "open_brain.services.embedding_health",
        "open_brain.services.embedding_helper",
        "open_brain.services.embedding_pin",
    } <= base_native._REQUIRED_MODULES


def test_rendered_pin_is_a_deterministic_constant_module() -> None:
    source = base_native.render_embedding_pin({"macos-arm64": DIGEST})
    namespace: dict[str, object] = {}
    exec(compile(source, "embedding_pin.py", "exec"), namespace)  # noqa: S102

    assert namespace["EXPECTED_EMBEDDING_INVENTORY_SHA256"] == {"macos-arm64": DIGEST}
    assert source == base_native.render_embedding_pin({"macos-arm64": DIGEST})
    assert "Mapping[str, str] = {}\n" in base_native.render_embedding_pin({})


@pytest.mark.parametrize(
    "pins",
    ({"windows": DIGEST}, {"macos-arm64": "ABC"}, {"macos-arm64": "0" * 63}),
)
def test_rendered_pin_rejects_invalid_values(pins: dict[str, str]) -> None:
    with pytest.raises(BaseNativeError, match="embedding pin"):
        base_native.render_embedding_pin(pins)


def test_spec_compiles_the_generated_pin_from_the_build_tree() -> None:
    spec = (ROOT / "release/open-brain/open-brain.spec").read_text(encoding="utf-8")

    assert "OPEN_BRAIN_EMBEDDING_PIN" in spec
    assert '"open_brain.services.embedding_pin"' in spec
    assert "code_cache[pin_name] = compile(" in spec


def test_pin_environment_is_written_below_the_build_output(tmp_path: Path) -> None:
    environment = base_native.embedding_pin_environment(tmp_path / "out", {"macos-arm64": DIGEST})
    generated = Path(environment["OPEN_BRAIN_EMBEDDING_PIN"])

    assert generated == (tmp_path / "out/generated/embedding_pin.py").resolve()
    assert generated.read_text(encoding="utf-8") == base_native.render_embedding_pin(
        {"macos-arm64": DIGEST}
    )
    source = ROOT / "packages/app/src/open_brain/services/embedding_pin.py"
    assert "EXPECTED_EMBEDDING_INVENTORY_SHA256: Mapping[str, str] = {}" in source.read_text(
        encoding="utf-8"
    )


def test_build_without_an_embedding_archive_drops_a_stale_pin_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Frozen(Exception):
        pass

    seen: list[dict[str, str] | None] = []

    def run(*_args: object, env: dict[str, str] | None = None, **_kwargs: object) -> None:
        seen.append(env)
        raise Frozen

    stale = tmp_path / "stale_pin.py"
    monkeypatch.setenv("OPEN_BRAIN_EMBEDDING_PIN", str(stale))
    monkeypatch.setenv("OPEN_BRAIN_UNRELATED_SETTING", "kept")
    monkeypatch.setattr(
        importlib.metadata,
        "version",
        lambda name: {"pyinstaller": "6.22.3", "pyinstaller-hooks-contrib": "2026.7"}[name],
    )
    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(Frozen):
        base_native.build_base_artifact(ROOT, tmp_path / "out")

    assert len(seen) == 1 and seen[0] is not None
    assert "OPEN_BRAIN_EMBEDDING_PIN" not in seen[0]
    assert seen[0]["OPEN_BRAIN_UNRELATED_SETTING"] == "kept"


@pytest.mark.parametrize(
    "module",
    ("numpy", "onnxruntime.capi", "tokenizers", "torch", "transformers", "sentence_transformers"),
)
def test_base_embedding_audit_refuses_ml_packages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, module: str
) -> None:
    modules = {*base_native._REQUIRED_MODULES}
    monkeypatch.setattr(base_native, "archive_modules", lambda _path: tuple(sorted(modules)))
    monkeypatch.setattr(
        base_native, "_embedded_pin_constants", lambda _path: frozenset({"macos-arm64", DIGEST})
    )
    artifact = tmp_path / "open-brain"

    base_native.audit_base_embedding(artifact, platform="macos-arm64", inventory_sha256=DIGEST)
    modules.add(module)
    with pytest.raises(BaseNativeError, match="machine-learning"):
        base_native.audit_base_embedding(artifact, platform="macos-arm64", inventory_sha256=DIGEST)


@pytest.mark.parametrize(
    "constants",
    (frozenset(), frozenset({"macos-arm64"}), frozenset({"macos-arm64", DIGEST, "2" * 64})),
)
def test_base_embedding_audit_requires_the_exact_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constants: frozenset[str]
) -> None:
    monkeypatch.setattr(
        base_native, "archive_modules", lambda _path: tuple(sorted(base_native._REQUIRED_MODULES))
    )
    monkeypatch.setattr(base_native, "_embedded_pin_constants", lambda _path: constants)
    with pytest.raises(BaseNativeError, match="pin"):
        base_native.audit_base_embedding(
            tmp_path / "open-brain", platform="macos-arm64", inventory_sha256=DIGEST
        )


def test_pin_constants_come_from_the_frozen_module_code() -> None:
    source = base_native.render_embedding_pin({"macos-arm64": DIGEST})
    code = compile(source, "embedding_pin.py", "exec")

    assert base_native._string_constants(code) >= {"macos-arm64", DIGEST}
    assert hashlib.sha256(DIGEST.encode()).hexdigest() not in base_native._string_constants(code)


# ------------------------------------------------------------- v2 formula

FIXTURE = ROOT / "tests/release/fixtures/manifest-v2/manifest.json"


def _four_role_manifest() -> bytes:
    resources = []
    for platform in v2.PLATFORMS:
        for role in v2.ROLES:
            resources.append(
                v2.ResourceV2(
                    role,
                    platform,
                    v2.archive_filename(role, "0.1.0", platform),
                    hashlib.sha256(f"archive {role} {platform}".encode()).hexdigest(),
                    1000,
                    v2.EXECUTABLES[role],
                    hashlib.sha256(f"exe {role}".encode()).hexdigest(),
                    v2.inventory_path(role),
                    hashlib.sha256(f"inv {role} {platform}".encode()).hexdigest(),
                )
            )
    return v2.render_manifest_v2(v2.ManifestV2("0.1.0", tuple(resources)))


def _render(tmp_path: Path, manifest: bytes, **options: object) -> str:
    source = tmp_path / v2.MANIFEST_FILENAME
    source.write_bytes(manifest)
    formula = base_native.render_homebrew_formula(source, tmp_path / "formula.rb", **options)  # type: ignore[arg-type]
    return formula.read_text(encoding="utf-8")


def _embedded_manifest(formula: str) -> bytes:
    body = formula.split("  COMPONENT_MANIFEST_V2 = <<~'JSON'.freeze\n", 1)[1].split(
        "\n  JSON\n", 1
    )[0]
    assert body.startswith("    ") and "\n" not in body
    return body[4:].encode("utf-8") + b"\n"


def test_v2_manifest_filename_is_shared_with_the_runtime() -> None:
    assert base_native.MANIFEST_V2_FILENAME == v2.MANIFEST_FILENAME
    assert f"share/open-brain/{v2.MANIFEST_FILENAME}" == eh.MANIFEST_PATH


def test_v2_formula_installs_every_resource_from_the_manifest(tmp_path: Path) -> None:
    manifest_bytes = FIXTURE.read_bytes()
    manifest = v2.parse_manifest_v2(manifest_bytes)
    formula = _render(tmp_path, manifest_bytes)

    assert formula.startswith("class OpenBrain < Formula\n")
    assert "keg_only" not in formula
    assert 'version "0.1.0"' in formula
    origin = "https://github.com/cbolden15/open-brain/releases/download/v0.1.0"
    for block, architecture, platform in (
        ("on_linux", "x86_64", "linux-x86_64"),
        ("on_macos", "arm64", "macos-arm64"),
    ):
        section = formula.split(f"  {block} do\n", 1)[1].split("\n  end\n", 1)[0]
        assert f"    depends_on arch: :{architecture}" in section
        records = {item.role: item for item in manifest.resources if item.platform == platform}
        assert f'    url "{origin}/{records["base"].filename}"' in section
        assert f'    sha256 "{records["base"].archive_sha256}"' in section
        for role in ("graphify", "embedding"):
            assert (
                f'    resource "{role}" do\n      url "{origin}/{records[role].filename}"\n'
                f'      sha256 "{records[role].archive_sha256}"\n    end'
            ) in section
        assert 'resource "harness"' not in section
    assert _embedded_manifest(formula) == manifest_bytes
    install = formula.split("  def install\n", 1)[1].split("\n  end\n", 1)[0]
    assert 'install_component(buildpath, records.fetch("base"), prefix)' in install
    assert 'install_component(Pathname.pwd, records.fetch("graphify"), prefix)' in install
    # The embedding tree holds no dylib for Homebrew to rewrite, so it is placed in install.
    placed = 'install_component(Pathname.pwd, records.fetch("embedding"), prefix)'
    # Every archive entry is checked against the inventory before the tree is copied.
    verified = 'verify_archive(resource("embedding").cached_download, records.fetch("embedding"))'
    assert install.index('resource("embedding").stage do') < install.index(verified)
    assert install.index(verified) < install.index(placed)
    assert f'(share/"open-brain/{v2.MANIFEST_FILENAME}").write COMPONENT_MANIFEST_V2' in install
    assert "def post_install" not in formula
    assert "pending" not in formula
    assert 'system "tar"' not in formula
    helpers = formula.split("  def install_component", 1)[1].split("\n  test do\n", 1)[0]
    assert 'target.chmod member["mode"]' in helpers
    assert "info.nlink != 1" in helpers
    assert "undeclared entry" in helpers
    for forbidden in (
        "ln_s",
        "install_symlink",
        "make_relative_symlink",
        "curl",
        'system "open-brain',
    ):
        assert forbidden not in install + helpers
    # Links are created only for inventory symlink members, never ad hoc (D14 option a).
    assert "File.symlink" not in install
    assert helpers.count("File.symlink(") == 1
    assert 'File.symlink(member["target"], target)' in helpers
    test = formula.split("  test do\n", 1)[1]
    assert 'operation:  "capabilities",' in test
    assert 'assert_equal Digest::SHA256.hexdigest(component), capabilities["component_id"]' in test
    assert '"embed"' not in test
    assert "open-brain-graphify --capabilities" in test
    assert "harness" not in test


def test_v2_formula_includes_the_harness_when_the_manifest_does(tmp_path: Path) -> None:
    formula = _render(tmp_path, _four_role_manifest(), smoke=True, base_url="file:///tmp/archives")

    assert formula.startswith("class OpenBrainSmoke < Formula\n")
    assert '  keg_only "Temporary contributor smoke fixture"' in formula
    assert formula.count('resource "harness" do') == 2
    assert 'install_component(Pathname.pwd, records.fetch("harness"), prefix)' in formula
    assert (
        'url "file:///tmp/archives/open-brain-components-v2-harness-0.1.0-macos-arm64.tar.gz"'
        in formula
    )
    assert "open-brain-harness/open-brain-harness harness --help" in formula


def test_v2_formula_refuses_an_invalid_manifest(tmp_path: Path) -> None:
    with pytest.raises(BaseNativeError, match="manifest v2 is invalid"):
        _render(tmp_path, FIXTURE.read_bytes() + b" ")


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is unavailable")
def test_v2_formula_is_valid_ruby(tmp_path: Path) -> None:
    for manifest in (FIXTURE.read_bytes(), _four_role_manifest()):
        _render(tmp_path, manifest)
        checked = subprocess.run(
            ("ruby", "-c", str(tmp_path / "formula.rb")),
            capture_output=True,
            text=True,
            check=False,
        )
        assert checked.returncode == 0, checked.stderr


_INVENTORY = "share/open-brain/inventories/embedding.json"


def _embedding_archive(path: Path, extra: tarfile.TarInfo | None = None) -> str:
    """A synthetic embedding archive; returns its inventory digest."""
    inventory = json.dumps(
        {
            "component": {},
            "members": [
                {"path": "libexec", "kind": "directory"},
                {"path": "libexec/helper", "kind": "file"},
                {"path": "share", "kind": "directory"},
                {"path": "share/open-brain", "kind": "directory"},
                {"path": "share/open-brain/inventories", "kind": "directory"},
            ],
        }
    ).encode()
    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as bundle:
        for name in ("libexec", "share", "share/open-brain", "share/open-brain/inventories"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            bundle.addfile(info)
        for name, data in (("libexec/helper", b"helper"), (_INVENTORY, inventory)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
        if extra is not None:
            bundle.addfile(extra, io.BytesIO(b"x" * extra.size) if extra.isreg() else None)
    return hashlib.sha256(inventory).hexdigest()


def _entry(name: str, kind: bytes = tarfile.REGTYPE) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type = kind
    if kind == tarfile.REGTYPE:
        info.size = 1
    else:
        info.linkname = "libexec/helper"
    return info


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is unavailable")
@pytest.mark.parametrize(
    ("extra", "accepted"),
    [
        (None, True),
        (_entry("/etc/open-brain"), False),
        (_entry("libexec/../../escape"), False),
        (_entry("libexec/link", tarfile.SYMTYPE), False),
        (_entry("libexec/hard", tarfile.LNKTYPE), False),
        (_entry("libexec/undeclared"), False),
    ],
)
def test_formula_verifies_every_embedding_archive_entry_before_extraction(
    tmp_path: Path, extra: tarfile.TarInfo | None, accepted: bool
) -> None:
    formula = _render(tmp_path, FIXTURE.read_bytes())
    method = (
        "  def verify_archive"
        + formula.split("  def verify_archive", 1)[1].split("\n  end\n", 1)[0]
    )
    script = tmp_path / "verify.rb"
    script.write_text(
        "def odie(message) = abort(message)\n"
        f"{method}\n  end\n"
        'require "json"\nverify_archive(ARGV[0], JSON.parse(ARGV[1]))\n',
        encoding="utf-8",
    )
    archive = tmp_path / "embedding.tar.gz"
    digest = _embedding_archive(archive, extra)
    record = {"role": "embedding", "inventory": _INVENTORY, "inventory_sha256": digest}
    checked = subprocess.run(
        ("ruby", str(script), str(archive), json.dumps(record)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert (checked.returncode == 0) is accepted, checked.stderr
    if not accepted:
        assert "open-brain: embedding archive" in checked.stderr


def _helper_script(tmp_path: Path, method: str, call: str) -> Path:
    formula = _render(tmp_path, FIXTURE.read_bytes())
    body = f"  def {method}" + formula.split(f"  def {method}", 1)[1].split("\n  end\n", 1)[0]
    script = tmp_path / f"{method}.rb"
    script.write_text(
        'require "fileutils"\nrequire "pathname"\n'
        "def odie(message) = abort(message)\n"
        f"{body}\n  end\n"
        f'require "json"\n{call}\n',
        encoding="utf-8",
    )
    return script


def _linked_tree(root: Path, *, target: str = "3.14") -> dict[str, object]:
    """A staged one-folder tree with a directory link; returns its manifest record."""
    framework = root / "libexec/open-brain/_internal/Python.framework"
    (framework / "Versions/3.14").mkdir(parents=True)
    (framework / "Versions/3.14/Python").write_bytes(b"python")
    (framework / "Versions/Current").symlink_to(target)
    (framework / "Python").symlink_to("Versions/Current/Python")
    (root / "share/open-brain/inventories").mkdir(parents=True)
    members = [
        {"path": "libexec", "kind": "directory", "mode": 0o755},
        {"path": "libexec/open-brain", "kind": "directory", "mode": 0o755},
        {"path": "libexec/open-brain/_internal", "kind": "directory", "mode": 0o755},
        {"path": str(framework.relative_to(root)), "kind": "directory", "mode": 0o755},
        {
            "path": str((framework / "Python").relative_to(root)),
            "kind": "symlink",
            "mode": 0o777,
            "target": "Versions/Current/Python",
        },
        {
            "path": str((framework / "Versions").relative_to(root)),
            "kind": "directory",
            "mode": 0o755,
        },
        {
            "path": str((framework / "Versions/3.14").relative_to(root)),
            "kind": "directory",
            "mode": 0o755,
        },
        {
            "path": str((framework / "Versions/3.14/Python").relative_to(root)),
            "kind": "file",
            "mode": 0o755,
            "bytes": 6,
            "sha256": hashlib.sha256(b"python").hexdigest(),
        },
        {
            "path": str((framework / "Versions/Current").relative_to(root)),
            "kind": "symlink",
            "mode": 0o777,
            "target": "3.14",
        },
        {"path": "share", "kind": "directory", "mode": 0o755},
        {"path": "share/open-brain", "kind": "directory", "mode": 0o755},
        {"path": "share/open-brain/inventories", "kind": "directory", "mode": 0o755},
    ]
    inventory = json.dumps({"members": members}).encode()
    (root / "share/open-brain/inventories/base.json").write_bytes(inventory)
    return {
        "role": "base",
        "inventory": "share/open-brain/inventories/base.json",
        "inventory_sha256": hashlib.sha256(inventory).hexdigest(),
    }


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is unavailable")
@pytest.mark.parametrize("target", ["3.14", "3.13"])
def test_formula_installs_symlink_members_exactly(tmp_path: Path, target: str) -> None:
    staged = tmp_path / "staged"
    record = _linked_tree(staged, target=target)
    record_target = "3.14"
    script = _helper_script(
        tmp_path,
        "install_component",
        "install_component(ARGV[0], JSON.parse(ARGV[1]), Pathname.new(ARGV[2]))",
    )
    prefix = tmp_path / "prefix"
    prefix.mkdir()
    checked = subprocess.run(
        ("ruby", str(script), str(staged), json.dumps(record), str(prefix)),
        capture_output=True,
        text=True,
        check=False,
    )
    if target != record_target:
        assert checked.returncode != 0
        assert "does not match its inventory" in checked.stderr
        return
    assert checked.returncode == 0, checked.stderr
    framework = prefix / "libexec/open-brain/_internal/Python.framework"
    assert (framework / "Versions/Current").is_symlink()
    assert (framework / "Versions/Current").readlink() == Path("3.14")
    assert (framework / "Python").readlink() == Path("Versions/Current/Python")
    assert (framework / "Python").read_bytes() == b"python"
    assert (framework / "Versions/3.14/Python").stat().st_mode & 0o777 == 0o755


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is unavailable")
def test_formula_refuses_a_regular_file_where_a_link_is_declared(tmp_path: Path) -> None:
    staged = tmp_path / "staged"
    record = _linked_tree(staged)
    current = staged / "libexec/open-brain/_internal/Python.framework/Python"
    current.unlink()
    current.write_bytes(b"python")
    script = _helper_script(
        tmp_path, "install_component", "install_component(ARGV[0], JSON.parse(ARGV[1]))"
    )
    checked = subprocess.run(
        ("ruby", str(script), str(staged), json.dumps(record)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert checked.returncode != 0
    assert "does not match its inventory" in checked.stderr


@pytest.mark.skipif(shutil.which("ruby") is None, reason="ruby is unavailable")
@pytest.mark.parametrize(
    ("linkname", "repeat", "accepted"),
    [("libexec/helper", False, True), ("other", False, False), ("libexec/helper", True, False)],
)
def test_formula_archive_check_admits_only_declared_links(
    tmp_path: Path, linkname: str, repeat: bool, accepted: bool
) -> None:
    inventory = json.dumps(
        {
            "members": [
                {"path": "libexec", "kind": "directory"},
                {"path": "libexec/alias", "kind": "symlink", "target": "libexec/helper"},
                {"path": "libexec/helper", "kind": "file"},
                {"path": "share", "kind": "directory"},
                {"path": "share/open-brain", "kind": "directory"},
                {"path": "share/open-brain/inventories", "kind": "directory"},
            ],
        }
    ).encode()
    archive = tmp_path / "base.tar.gz"
    with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as bundle:
        for name in ("libexec", "share", "share/open-brain", "share/open-brain/inventories"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            bundle.addfile(info)
        alias = tarfile.TarInfo("libexec/alias")
        alias.type = tarfile.SYMTYPE
        alias.linkname = linkname
        bundle.addfile(alias)
        if repeat:
            # A later same-name entry must not replace the checked link.
            shadow = tarfile.TarInfo("libexec/alias")
            shadow.size = 1
            bundle.addfile(shadow, io.BytesIO(b"x"))
        for name, data in (("libexec/helper", b"helper"), (_INVENTORY, inventory)):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    record = {
        "role": "base",
        "inventory": _INVENTORY,
        "inventory_sha256": hashlib.sha256(inventory).hexdigest(),
    }
    script = _helper_script(
        tmp_path, "verify_archive", "verify_archive(ARGV[0], JSON.parse(ARGV[1]))"
    )
    checked = subprocess.run(
        ("ruby", str(script), str(archive), json.dumps(record)),
        capture_output=True,
        text=True,
        check=False,
    )
    assert (checked.returncode == 0) is accepted, checked.stderr
    if repeat:
        assert "repeats" in checked.stderr


# ----------------------------------------------------- v2 archives and smoke


V1_EXECUTABLES = {"open-brain", "open-brain/open-brain", "open-brain/_internal/tool"}
V1_LINKS = {"open-brain/_internal/alias": "data.txt"}


def _v1_archive(
    path: Path,
    members: dict[str, bytes],
    *,
    link: str | None = None,
    links: dict[str, str] | None = None,
    hardlink: str | None = None,
) -> Path:
    linked = {**(links or {}), **({link: "open-brain"} if link is not None else {})}
    directories = {
        str(parent)
        for name in (*members, *linked)
        for parent in Path(name).parents
        if str(parent) != "."
    }
    with tarfile.open(path, "w:gz", format=tarfile.USTAR_FORMAT) as bundle:
        for name in sorted({*directories, *members, *linked}):
            info = tarfile.TarInfo(name)
            if name in directories:
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                bundle.addfile(info)
            elif name in linked:
                info.type = tarfile.SYMTYPE
                info.mode = 0o777
                info.linkname = linked[name]
                bundle.addfile(info)
            else:
                info.size = len(members[name])
                info.mode = 0o755 if name in V1_EXECUTABLES else 0o644
                bundle.addfile(info, io.BytesIO(members[name]))
        if hardlink is not None:
            info = tarfile.TarInfo(hardlink)
            info.type = tarfile.LNKTYPE
            info.linkname = "open-brain/open-brain"
            bundle.addfile(info)
    return path


BASE_V1 = {
    "open-brain/open-brain": b"synthetic base",
    "open-brain/_internal/data.txt": b"data",
    "open-brain/_internal/tool": b"tool",
    "obsidian-plugin/main.js": b"main",
    "obsidian-plugin/manifest.json": b"{}",
    "obsidian-plugin/styles.css": b"css",
}


def test_v1_base_archive_converts_to_prefix_relative_v2(tmp_path: Path) -> None:
    archive = _v1_archive(
        tmp_path / "open-brain-0.1.0-macos-arm64.tar.gz", BASE_V1, links=V1_LINKS
    )
    resource = release_v2.convert_v1_archive(
        archive, role="base", version="0.1.0", platform="macos-arm64", output=tmp_path / "v2"
    )
    converted = tmp_path / "v2" / v2.archive_filename("base", "0.1.0", "macos-arm64")

    assert resource.filename == converted.name
    assert resource.archive_sha256 == hashlib.sha256(converted.read_bytes()).hexdigest()
    assert resource.executable_sha256 == hashlib.sha256(b"synthetic base").hexdigest()
    with tarfile.open(converted) as bundle:
        inventory_bytes = bundle.extractfile(v2.inventory_path("base")).read()  # type: ignore[union-attr]
    inventory = v2.parse_inventory(inventory_bytes)
    assert {member.path for member in inventory.members if member.kind == "file"} == {
        "libexec/open-brain/open-brain",
        "libexec/open-brain/_internal/data.txt",
        "libexec/open-brain/_internal/tool",
        "share/open-brain/obsidian-plugin/main.js",
        "share/open-brain/obsidian-plugin/manifest.json",
        "share/open-brain/obsidian-plugin/styles.css",
    }
    assert {m.path: m.target for m in inventory.members if m.kind == "symlink"} == {
        "bin/open-brain": "../libexec/open-brain/open-brain",
        "libexec/open-brain/_internal/alias": "data.txt",
    }
    modes = {member.path: member.mode for member in inventory.members}
    assert modes["libexec/open-brain/_internal/tool"] == 0o755
    assert modes["libexec/open-brain/_internal/data.txt"] == 0o644
    v2.validate_resource_inventory(resource, "0.1.0", inventory)
    destination = tmp_path / "prefix"
    v2.safe_extract(converted, destination, inventory)
    v2.verify_extracted_resource(destination, inventory)
    assert (destination / "bin/open-brain").read_bytes() == b"synthetic base"
    record = converted.with_name(converted.name.removesuffix(".tar.gz") + ".resource.json")
    assert json.loads(record.read_bytes())["inventory_sha256"] == resource.inventory_sha256


@pytest.mark.parametrize(
    ("members", "options"),
    (
        ({**BASE_V1, "unexpected.txt": b"x"}, {}),
        (BASE_V1, {"link": "obsidian-plugin/link"}),
        (BASE_V1, {"links": {"open-brain/_internal/evil": "../../../../outside"}}),
        (BASE_V1, {"links": {"open-brain/_internal/evil": "missing"}}),
        (BASE_V1, {"hardlink": "open-brain/_internal/hard"}),
        ({key: value for key, value in BASE_V1.items() if key != "open-brain/open-brain"}, {}),
    ),
)
def test_v1_conversion_refuses_unmapped_linked_or_incomplete_archives(
    tmp_path: Path, members: dict[str, bytes], options: dict[str, object]
) -> None:
    archive = _v1_archive(
        tmp_path / "open-brain-0.1.0-macos-arm64.tar.gz",
        members,
        **options,  # type: ignore[arg-type]
    )
    with pytest.raises(release_v2.ReleaseV2Error):
        release_v2.convert_v1_archive(
            archive, role="base", version="0.1.0", platform="macos-arm64", output=tmp_path / "v2"
        )


def test_smoke_mirror_completes_a_single_host_manifest(tmp_path: Path) -> None:
    manifest = v2.parse_manifest_v2(FIXTURE.read_bytes())
    host = [item for item in manifest.resources if item.platform == "macos-arm64"]

    resources = release_v2.mirror_for_smoke(host, "0.1.0")
    written = release_v2.write_manifest_v2(resources, "0.1.0", tmp_path / v2.MANIFEST_FILENAME)
    parsed = v2.parse_manifest_v2(written.read_bytes())

    linux = [item for item in parsed.resources if item.platform == "linux-x86_64"]
    assert [item.role for item in linux] == ["base", "graphify", "embedding"]
    assert [item.filename for item in linux] == [
        v2.archive_filename(role, "0.1.0", "linux-x86_64")
        for role in ("base", "graphify", "embedding")
    ]
    assert [item for item in parsed.resources if item.platform == "macos-arm64"] == host
    with pytest.raises(release_v2.ReleaseV2Error, match="exactly one"):
        release_v2.mirror_for_smoke(manifest.resources, "0.1.0")


def test_homebrew_smoke_renders_v2_and_checks_the_embedding_when_given_an_archive() -> None:
    script = (ROOT / "tools/homebrew-smoke.sh").read_text(encoding="utf-8")
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "embedding_archive=${4:-}" in script
    assert "python -m tools.open_brain_dev.release_v2 smoke-release" in script
    assert 'brew test "$smoke_formula"' in script
    assert "--embedding-artifact" in script
    assert script.count("brew install") == 1
    assert (
        "$(EMBEDDING_ARCHIVE_OPTION)"
        in makefile.split("native-audit: native\n", 1)[1].split("\n\n")[0]
    )
    assert '$(if $(wildcard $(EMBEDDING_ARCHIVE)),"$(abspath $(EMBEDDING_ARCHIVE))")' in makefile
    assert "make embedding-native` writes the archive, then" in makefile
