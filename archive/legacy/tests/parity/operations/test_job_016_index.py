import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path

import pytest
from open_brain_engine.core.ids import canonical_json_bytes
from open_brain_engine.core.models import Authority, PrivacyDecision, PrivacyReason, PrivacyTier
from open_brain_engine.engine import LockScope

from open_brain_legacy.operations.catalog import get_job
from open_brain_legacy.operations.index import IndexPrivacyError, IndexRoots, build_index
from open_brain_legacy.operations.models import DeploymentTarget, HostRole


class RecordingLease:
    def __init__(self) -> None:
        self.scopes: list[LockScope] = []

    @contextmanager
    def acquire(self, scope: LockScope) -> Iterator[None]:
        self.scopes.append(scope)
        yield


class DeterministicEmbedder:
    model_id = "synthetic-local-v1"
    requires_cloud_authority = False
    requires_external_egress = False

    def __init__(self, on_embed: Callable[[str], None] | None = None) -> None:
        self.calls: list[str] = []
        self._on_embed = on_embed

    def embed(self, text: str) -> tuple[float, ...]:
        self.calls.append(text)
        if self._on_embed is not None:
            self._on_embed(text)
        if "explode" in text:
            raise RuntimeError("synthetic embedding failure")
        digest = sha256(text.encode("utf-8")).digest()
        return tuple(round(value / 255, 8) for value in digest[:4])


def _roots(tmp_path: Path) -> IndexRoots:
    pages = tmp_path / "pages"
    captures = tmp_path / "captures"
    output = tmp_path / "index-output"
    (pages / "notes").mkdir(parents=True)
    (captures / "daily").mkdir(parents=True)
    output.mkdir()
    return IndexRoots(pages_root=pages, captures_root=captures, output_root=output)


def _privacy(*, cloud: bool = False, egress: bool = False) -> PrivacyDecision:
    if cloud:
        return PrivacyDecision.create(
            tier=PrivacyTier.PERSONAL,
            reason=PrivacyReason.PERSONAL_CONFIRMED,
            policy_version="privacy-v1",
            authority=Authority(cloud=True, external_egress=egress),
            confirmation_ref="confirmation.synthetic-001",
        )
    return PrivacyDecision.create(
        tier=PrivacyTier.PERSONAL,
        reason=PrivacyReason.PERSONAL_LOCAL_ONLY,
        policy_version="privacy-v1",
        authority=Authority(cloud=False, external_egress=False),
    )


def _rows(database: Path) -> tuple[list[tuple[str, str, str, str]], list[tuple[str, str]]]:
    with sqlite3.connect(database) as connection:
        chunks = connection.execute(
            "SELECT chunk_id, source_path, content_sha256, content FROM chunks "
            "ORDER BY source_path, ordinal"
        ).fetchall()
        fts = connection.execute(
            "SELECT chunk_id, content FROM chunks_fts ORDER BY chunk_id"
        ).fetchall()
    return chunks, fts


def _chunk_id(source_path: str, content: str) -> tuple[str, str]:
    content_hash = sha256(content.encode("utf-8")).hexdigest()
    identity = {
        "identity_version": 1,
        "source_path": source_path,
        "ordinal": 0,
        "content_sha256": content_hash,
    }
    return "chunk_" + sha256(canonical_json_bytes(identity)).hexdigest(), content_hash


def test_job_016_rebuilds_atomic_deterministic_fts_and_reuses_embeddings(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    page_content = "# Alpha\n\nSynthetic work page.\n"
    capture_content = '{"text":"Synthetic capture"}\n'
    (roots.pages_root / "notes" / "alpha.md").write_text(
        page_content.replace("\n", "\r\n"), encoding="utf-8"
    )
    (roots.captures_root / "daily" / "capture.json").write_text(
        capture_content, encoding="utf-8"
    )
    database = roots.output_root / roots.database_name

    def observe_temporary_build(_text: str) -> None:
        assert database.exists() is False
        assert any(
            path.name.startswith(f".{roots.database_name}.") and path.name.endswith(".tmp")
            for path in roots.output_root.iterdir()
        )

    embedder = DeterministicEmbedder(on_embed=observe_temporary_build)
    lease = RecordingLease()
    job = get_job("JOB-016")

    first = build_index(
        target=job.deployment_target,
        host_role=job.host_role,
        roots=roots,
        lease=lease,
        embedder=embedder,
        privacy=_privacy(),
    )
    first_rows = _rows(database)

    expected = []
    for source_path, content in (
        ("captures/daily/capture.json", capture_content),
        ("pages/notes/alpha.md", page_content),
    ):
        chunk_id, content_hash = _chunk_id(source_path, content)
        expected.append((chunk_id, source_path, content_hash, content))

    assert job.deployment_target is DeploymentTarget.CANONICAL_WRITER
    assert job.host_role is HostRole.WRITER
    assert first.document_count == 2
    assert first.chunk_count == 2
    assert first.embeddings_created == 2
    assert first.embeddings_reused == 0
    assert first_rows[0] == expected
    assert first_rows[1] == sorted((row[0], row[3]) for row in expected)
    assert not list(roots.output_root.glob(f".{roots.database_name}.*.tmp*"))

    embedder._on_embed = None
    second = build_index(
        target=job.deployment_target,
        host_role=job.host_role,
        roots=roots,
        lease=lease,
        embedder=embedder,
        privacy=_privacy(),
    )

    assert second.generation_id == first.generation_id
    assert second.embeddings_created == 0
    assert second.embeddings_reused == 2
    assert embedder.calls == [capture_content, page_content]
    assert _rows(database) == first_rows
    assert lease.scopes == [LockScope.INDEX, LockScope.INDEX]


def test_job_016_failed_build_preserves_previous_generation_and_cleans_temporary_files(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    (roots.pages_root / "notes" / "stable.md").write_text(
        "Stable synthetic page.\n", encoding="utf-8"
    )
    lease = RecordingLease()
    job = get_job("JOB-016")
    stable = build_index(
        target=job.deployment_target,
        host_role=job.host_role,
        roots=roots,
        lease=lease,
        embedder=DeterministicEmbedder(),
        privacy=_privacy(),
    )
    database = roots.output_root / roots.database_name
    stable_rows = _rows(database)

    (roots.captures_root / "daily" / "explode.txt").write_text(
        "explode synthetic build", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="synthetic embedding failure"):
        build_index(
            target=job.deployment_target,
            host_role=job.host_role,
            roots=roots,
            lease=lease,
            embedder=DeterministicEmbedder(),
            privacy=_privacy(),
        )

    assert _rows(database) == stable_rows
    with sqlite3.connect(database) as connection:
        generation_id = connection.execute(
            "SELECT value FROM metadata WHERE key = 'generation_id'"
        ).fetchone()[0]
    assert generation_id == stable.generation_id
    assert not list(roots.output_root.glob(f".{roots.database_name}.*.tmp*"))


def test_job_016_excludes_symlinks_without_following_file_or_directory_targets(
    tmp_path: Path,
) -> None:
    roots = _roots(tmp_path)
    regular_content = "Canonical synthetic page.\n"
    (roots.pages_root / "notes" / "canonical.md").write_text(
        regular_content,
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "external.md"
    outside_file.write_text("External content must not be indexed.\n", encoding="utf-8")
    outside_directory = outside / "directory"
    outside_directory.mkdir()
    (outside_directory / "nested.json").write_text(
        '{"text":"External directory content must not be indexed"}\n',
        encoding="utf-8",
    )
    (roots.pages_root / "notes" / "external-file.md").symlink_to(outside_file)
    (roots.captures_root / "daily" / "external-directory").symlink_to(
        outside_directory,
        target_is_directory=True,
    )

    job = get_job("JOB-016")
    built = build_index(
        target=job.deployment_target,
        host_role=job.host_role,
        roots=roots,
        lease=RecordingLease(),
        embedder=DeterministicEmbedder(),
        privacy=_privacy(),
    )
    chunks, _fts = _rows(roots.output_root / roots.database_name)

    assert built.document_count == 1
    assert built.chunk_count == 1
    assert chunks[0][1:] == (
        "pages/notes/canonical.md",
        sha256(regular_content.encode("utf-8")).hexdigest(),
        regular_content,
    )


def test_job_016_denied_cloud_embedder_never_receives_content(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    (roots.pages_root / "notes" / "private.md").write_text(
        "Synthetic private page", encoding="utf-8"
    )
    embedder = DeterministicEmbedder()
    embedder.requires_cloud_authority = True
    embedder.requires_external_egress = True
    job = get_job("JOB-016")

    with pytest.raises(IndexPrivacyError, match="authority denied"):
        build_index(
            target=job.deployment_target,
            host_role=job.host_role,
            roots=roots,
            lease=RecordingLease(),
            embedder=embedder,
            privacy=_privacy(),
        )

    assert embedder.calls == []
