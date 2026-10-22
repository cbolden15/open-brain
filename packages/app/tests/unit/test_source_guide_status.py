"""`open-brain source guide` dispatch before root selection and read-only status."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import cast

import pytest

from open_brain.profile import compile_single_user_local
from open_brain.services import local_entrypoints, source_guide
from open_brain.services.source_guide import GuidePaths, guide_status

ENVELOPE_KEYS = {
    "schema_version",
    "workflow_id",
    "revision",
    "action",
    "outcome",
    "code",
    "intent",
    "data",
    "next_actions",
    "retry_after_epoch",
}
SOURCE_ID = "slack.0123456789abcdef01234567"


def _installed_collector() -> Path:
    path = Path(sys.executable).parent / "open-brain-collector"
    if not path.is_file():
        pytest.skip("workspace collector is not installed")
    return path


@pytest.fixture(autouse=True)
def _no_bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("guide status must not select or bootstrap a Brain")

    monkeypatch.setattr(local_entrypoints, "select_local_root", refuse)
    monkeypatch.setattr(local_entrypoints, "_parser", refuse)


def _common(tmp_path: Path, executable: Path | None = None) -> list[str]:
    return [
        "--data-dir",
        str(tmp_path / "brain"),
        "--collector-state",
        str(tmp_path / "collector" / "state.json"),
        "--workflow-state",
        str(tmp_path / "guide" / "journal.json"),
        "--collector-executable",
        str(executable or tmp_path / "missing-collector"),
    ]


def _run(
    capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> tuple[int, dict[str, object] | None, str]:
    code = local_entrypoints.run_cli(["source", "guide", *arguments])
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.startswith("{") else None
    return code, payload, captured.err


def test_missing_collector_is_an_actionable_refusal_before_bootstrap(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, payload, _ = _run(capsys, ["status", *_common(tmp_path), "--json"])

    assert code == 78
    assert payload is not None and set(payload) == ENVELOPE_KEYS
    assert payload["action"] == "status"
    assert payload["outcome"] == "refused"
    assert payload["code"] == "collector_unavailable"
    assert payload["data"] is None
    assert not (tmp_path / "brain").exists()

    code, payload, error = _run(capsys, ["status", *_common(tmp_path)])
    assert (code, payload) == (78, None)
    assert "open-brain-collector" in error


@pytest.mark.parametrize(
    "mutation",
    [
        lambda items: [*items[:1], "relative", *items[2:]],
        lambda items: [*items, "--data-dir", items[1]],
        lambda items: [*items, "--yes"],
        lambda items: items[2:],
        lambda items: [*items[:5], items[1], *items[6:]],
        lambda items: [*items[:5], str(Path(items[1]) / "guide.json"), *items[6:]],
    ],
)
def test_invalid_or_aliased_arguments_refuse_before_any_launch(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], mutation: object
) -> None:
    arguments = cast(list[str], mutation(_common(tmp_path)))  # type: ignore[operator]
    code, payload, _ = _run(capsys, ["status", *arguments, "--json"])
    assert code == 2
    assert payload is not None
    assert payload["outcome"] == "refused"
    assert payload["code"] == "invalid_request"


def test_unknown_leaf_is_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code, payload, _ = _run(capsys, ["reset", *_common(tmp_path), "--json"])
    assert code == 2
    assert payload is not None and payload["code"] == "invalid_request"


def test_installed_collector_without_journal_is_uninitialized_and_creates_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    compile_single_user_local(tmp_path / "brain")
    before = sorted(str(path) for path in tmp_path.rglob("*"))

    code, payload, _ = _run(
        capsys, ["status", *_common(tmp_path, _installed_collector()), "--json"]
    )

    assert code == 78
    assert payload is not None and payload["code"] == "state_uninitialized"
    assert sorted(str(path) for path in tmp_path.rglob("*")) == before


def test_existing_journal_is_not_reinterpreted_by_this_build(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    compile_single_user_local(tmp_path / "brain")
    (tmp_path / "guide").mkdir(mode=0o700)
    (tmp_path / "guide" / "journal.json").write_text("{}")

    code, payload, _ = _run(
        capsys, ["status", *_common(tmp_path, _installed_collector()), "--json"]
    )

    assert code == 78
    assert payload is not None and payload["code"] == "journal_incompatible"


@pytest.mark.parametrize("leaf", ["slack", "advance"])
def test_write_phases_refuse_as_not_implemented(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    leaf: str,
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("no collector launch")

    monkeypatch.setattr(source_guide, "resolve_collector_executable", refuse)
    extra = (
        ["--decision-file", str(tmp_path / "decision.json"), "--json"] if leaf == "advance" else []
    )
    code, payload, error = _run(capsys, [leaf, *_common(tmp_path), *extra])

    assert code == 78
    if leaf == "advance":
        assert payload is not None
        assert payload["outcome"] == "refused"
        assert payload["action"] == "advance"
    else:
        assert "not implemented in this build" in error
    code, payload, _ = _run(capsys, ["slack", *_common(tmp_path), "--json"])
    assert code == 2


def test_status_renders_versioned_health_from_the_installed_collector(tmp_path: Path) -> None:
    compile_single_user_local(tmp_path / "brain")
    paths = GuidePaths(
        brain=tmp_path / "brain",
        state=tmp_path / "collector" / "state.json",
        journal=tmp_path / "guide" / "journal.json",
        executable=_installed_collector(),
    )

    result = guide_status(paths, selected_source_id=SOURCE_ID)

    assert set(result) == ENVELOPE_KEYS
    assert result["outcome"] == "completed"
    data = cast(dict[str, object], result["data"])
    assert data["kind"] == "health"
    report = cast(dict[str, object], data["report"])
    assert cast(dict[str, object], report["source"])["source_id"] == SOURCE_ID
    assert cast(dict[str, object], report["snapshot"])["consistency"] == "uninitialized"
    assert "Global consistency: unknown" in source_guide.render_status(result)
