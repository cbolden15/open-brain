from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from open_brain_engine.engine import PublicJobCaptureContext, open_local_engine

from open_brain.profile import compile_single_user_local
from open_brain_connectors.runtime.connectors import (
    ConnectorBudget,
    ConnectorBudgetLimits,
    ConnectorCaptureSink,
    ConnectorContractError,
    ConnectorOutcome,
    ConnectorRunEvidence,
)
from open_brain_connectors.runtime.github import (
    GitHubPageStatus,
    GitHubRepositoryCheckpoint,
    GitHubRepositoryCheckpointStore,
    GitHubRepositoryPage,
    GitHubSourceAdapter,
)

from .test_source_intake import _privacy


def test_github_adapter_builds_selection_preview_and_metadata_only_records() -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:open-brain-test",
        owner="cbolden15",
        repository="open-brain-fixture",
    )
    issue = adapter.issue_from_rest(
        {
            "number": 42,
            "html_url": "https://github.com/cbolden15/open-brain-fixture/issues/42",
            "title": "Fixture issue",
            "body": "Synthetic issue body from the disposable D2 fixture.",
            "updated_at": "2026-09-14T12:00:00Z",
        }
    )
    pull = adapter.issue_from_rest(
        {
            "number": 7,
            "html_url": "https://github.com/cbolden15/open-brain-fixture/pull/7",
            "title": "Fixture pull request",
            "body": "Synthetic pull request body from the disposable D2 fixture.",
            "updated_at": "2026-09-14T12:01:00Z",
            "pull_request": {"html_url": "https://github.com/cbolden15/open-brain-fixture/pull/7"},
        }
    )
    comment = adapter.comment_from_rest(
        {
            "id": 987,
            "issue_url": "https://api.github.com/repos/cbolden15/open-brain-fixture/issues/42",
            "html_url": (
                "https://github.com/cbolden15/open-brain-fixture/issues/42#issuecomment-987"
            ),
            "body": "Synthetic issue comment body from the disposable D2 fixture.",
            "updated_at": "2026-09-14T12:02:00Z",
        }
    )

    page = adapter.preview_repository(
        selection,
        (issue, pull, comment),
        privacy=_privacy(),
        next_cursor="cursor:page2",
    )
    encoded = page.to_dict()

    assert selection.resource_id == "repo:cbolden15/open-brain-fixture"
    assert encoded["next_cursor"] == "cursor:page2"
    records = encoded["records"]
    assert isinstance(records, list)
    assert [record["content_type"] for record in records] == [
        "issue",
        "pull_request",
        "comment",
    ]
    assert "Synthetic issue body" not in repr(encoded)
    assert all(
        set(record) == {"content_type", "delivery_id", "selected", "source_reference", "title"}
        for record in records
    )


def test_github_adapter_accepts_only_non_secret_connection_references() -> None:
    connection = GitHubSourceAdapter().connection_ref(
        connection_id="account:open-brain-test",
        account_login="open-brain-test",
        credential_ref="keychain:open-brain/github/open-brain-test",
        public_onboarding_proof=True,
    )

    assert connection.connection_id == "account:open-brain-test"
    assert connection.public_onboarding_proof is True
    with pytest.raises(ConnectorContractError, match="invalid github connection"):
        GitHubSourceAdapter().connection_ref(
            connection_id="account:open-brain-test",
            account_login="open-brain-test",
            credential_ref="ghp_plain_token_is_not_a_reference",
            public_onboarding_proof=True,
        )
    with pytest.raises(ConnectorContractError, match="invalid github connection"):
        GitHubSourceAdapter().connection_ref(
            connection_id="account:open-brain-test",
            account_login="open-brain-test",
            credential_ref="session:ghu_plain_user_token_is_not_a_reference",
            public_onboarding_proof=True,
        )


def test_github_adapter_models_public_device_auth_without_token_material() -> None:
    session = GitHubSourceAdapter().device_auth_session(
        device_code_ref="session:github-device-flow/fixture",
        user_code="ABCD-1234",
        verification_uri="https://github.com/login/device",
        expires_in_seconds=900,
        interval_seconds=5,
    )

    assert session.verification_uri == "https://github.com/login/device"
    assert "ghp_" not in repr(session)
    with pytest.raises(ConnectorContractError, match="invalid github auth session"):
        GitHubSourceAdapter().device_auth_session(
            device_code_ref="session:github-device-flow/fixture",
            user_code="abcd-1234",
            verification_uri="https://github.com/login/oauth/authorize",
            expires_in_seconds=900,
            interval_seconds=5,
        )


@pytest.mark.parametrize(
    "connection_id",
    (
        "ghp_plain_token_is_not_a_reference",
        "account:ghp_plain_token_is_not_a_reference",
        "account:github_pat_plain_token_is_not_a_reference",
    ),
)
def test_github_repository_selection_rejects_secret_shaped_connection_ids(
    connection_id: str,
) -> None:
    with pytest.raises(ConnectorContractError, match="invalid github connection"):
        GitHubSourceAdapter().repository_selection(
            connection_id=connection_id,
            owner="fixture",
            repository="project",
        )


def test_github_repository_page_and_checkpoint_advance_after_exact_ack() -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:open-brain-test",
        owner="cbolden15",
        repository="open-brain-fixture",
    )
    page = adapter.repository_page_from_rest(
        selection,
        (
            {
                "number": 42,
                "html_url": "https://github.com/cbolden15/open-brain-fixture/issues/42",
                "title": "Fixture issue",
                "body": "Synthetic issue body from the disposable D2 fixture.",
                "updated_at": "2026-09-14T12:00:00Z",
            },
            {
                "id": 987,
                "issue_url": (
                    "https://api.github.com/repos/cbolden15/open-brain-fixture/issues/42"
                ),
                "html_url": (
                    "https://github.com/cbolden15/open-brain-fixture/issues/42"
                    "#issuecomment-987"
                ),
                "body": "Synthetic issue comment body from the disposable D2 fixture.",
                "updated_at": "2026-09-14T12:02:00Z",
            },
        ),
        privacy=_privacy(),
        next_cursor="cursor:page2",
    )
    assert page.status is GitHubPageStatus.READY
    assert page.preview is not None
    delivery_ids = tuple(record.delivery_id for record in page.preview.records)

    checkpoint = GitHubRepositoryCheckpoint.initial(selection).advance(
        page.preview,
        committed_delivery_ids=delivery_ids,
    )

    assert checkpoint.next_cursor == "cursor:page2"
    assert checkpoint.committed_delivery_ids == delivery_ids
    assert GitHubRepositoryCheckpoint.from_dict(checkpoint.to_dict()) == checkpoint
    assert "Synthetic issue body" not in repr(checkpoint.to_dict())


def test_github_repository_checkpoint_store_persists_atomically_after_ack(
    tmp_path: Path,
) -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    page = adapter.preview_repository(
        selection,
        (
            adapter.issue_from_rest(
                {
                    "number": 1,
                    "html_url": "https://github.com/fixture/project/issues/1",
                    "title": "One",
                    "body": "Synthetic first body.",
                    "updated_at": "2026-09-14T12:00:00Z",
                }
            ),
        ),
        privacy=_privacy(),
        next_cursor="cursor:page2",
    )
    store = GitHubRepositoryCheckpointStore(tmp_path / "checkpoints")

    loaded = store.load(selection)
    assert loaded == GitHubRepositoryCheckpoint.initial(selection)

    checkpoint = loaded.advance(
        page,
        committed_delivery_ids=tuple(record.delivery_id for record in page.records),
    )
    path = store.save(checkpoint)

    assert path.read_text(encoding="utf-8").endswith("\n")
    assert store.load(selection) == checkpoint
    assert "Synthetic first body" not in path.read_text(encoding="utf-8")


def test_github_repository_import_submits_selected_intake_then_advances_checkpoint(
    tmp_path: Path,
) -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    records = (
        adapter.issue_from_rest(
            {
                "number": 1,
                "html_url": "https://github.com/fixture/project/issues/1",
                "title": "One",
                "body": "Synthetic first issue body.",
                "updated_at": "2026-09-14T12:00:00Z",
            }
        ),
        adapter.comment_from_rest(
            {
                "id": 25,
                "issue_url": "https://api.github.com/repos/fixture/project/issues/1",
                "html_url": "https://github.com/fixture/project/issues/1#issuecomment-25",
                "body": "Synthetic first comment body.",
                "updated_at": "2026-09-14T12:05:00Z",
            }
        ),
    )
    page = adapter.repository_page_from_rest(
        selection,
        (
            {
                "number": 1,
                "html_url": "https://github.com/fixture/project/issues/1",
                "title": "One",
                "body": "Synthetic first issue body.",
                "updated_at": "2026-09-14T12:00:00Z",
            },
            {
                "id": 25,
                "issue_url": "https://api.github.com/repos/fixture/project/issues/1",
                "html_url": "https://github.com/fixture/project/issues/1#issuecomment-25",
                "body": "Synthetic first comment body.",
                "updated_at": "2026-09-14T12:05:00Z",
            },
        ),
        privacy=_privacy(),
        next_cursor="cursor:page2",
    )
    assert page.preview is not None
    intakes = tuple(adapter.intake(selection, record, privacy=_privacy()) for record in records)

    advanced, receipt = adapter.import_repository_page(
        GitHubRepositoryCheckpoint.initial(selection),
        page,
        intakes,
        _capture_sink(tmp_path),
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert receipt.submitted_count == 2
    assert receipt.created_count == 2
    assert receipt.duplicate_count == 0
    assert receipt.checkpoint_committed is True
    assert advanced.next_cursor == "cursor:page2"
    assert advanced.committed_delivery_ids == tuple(
        record.delivery_id for record in page.preview.records
    )


def test_github_repository_import_replays_after_crash_before_checkpoint_without_duplicates(
    tmp_path: Path,
) -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    record = adapter.issue_from_rest(
        {
            "number": 1,
            "html_url": "https://github.com/fixture/project/issues/1",
            "title": "One",
            "body": "Synthetic replay issue body.",
            "updated_at": "2026-09-14T12:00:00Z",
        }
    )
    page = adapter.repository_page_from_rest(
        selection,
        (
            {
                "number": 1,
                "html_url": "https://github.com/fixture/project/issues/1",
                "title": "One",
                "body": "Synthetic replay issue body.",
                "updated_at": "2026-09-14T12:00:00Z",
            },
        ),
        privacy=_privacy(),
    )
    intake = adapter.intake(selection, record, privacy=_privacy())
    initial = GitHubRepositoryCheckpoint.initial(selection)
    sink = _capture_sink(tmp_path)

    adapter.import_repository_page(initial, page, (intake,), sink)
    advanced, receipt = adapter.import_repository_page(initial, page, (intake,), sink)

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert receipt.submitted_count == 1
    assert receipt.created_count == 0
    assert receipt.duplicate_count == 1
    assert page.preview is not None
    assert advanced.committed_delivery_ids == tuple(
        record.delivery_id for record in page.preview.records
    )


def test_github_repository_import_syncs_changed_revision_after_checkpoint(
    tmp_path: Path,
) -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    first_record = adapter.issue_from_rest(
        {
            "number": 1,
            "html_url": "https://github.com/fixture/project/issues/1",
            "title": "One",
            "body": "Synthetic original issue body.",
            "updated_at": "2026-09-14T12:00:00Z",
        }
    )
    first_page = adapter.preview_repository(
        selection,
        (first_record,),
        privacy=_privacy(),
    )
    first_intake = adapter.intake(selection, first_record, privacy=_privacy())
    sink = _capture_sink(tmp_path)
    checkpoint, receipt = adapter.import_repository_page(
        GitHubRepositoryCheckpoint.initial(selection),
        GitHubRepositoryPage(status=GitHubPageStatus.READY, preview=first_page),
        (first_intake,),
        sink,
    )

    assert receipt.created_count == 1
    assert checkpoint.committed_revision_identities == (
        first_intake.key.revision_identity(),
    )

    same_checkpoint, same_receipt = adapter.import_repository_page(
        checkpoint,
        GitHubRepositoryPage(status=GitHubPageStatus.READY, preview=first_page),
        (first_intake,),
        sink,
    )

    assert same_checkpoint == checkpoint
    assert same_receipt.outcome is ConnectorOutcome.EMPTY
    assert same_receipt.submitted_count == 0

    changed_record = adapter.issue_from_rest(
        {
            "number": 1,
            "html_url": "https://github.com/fixture/project/issues/1",
            "title": "One",
            "body": "Synthetic updated issue body.",
            "updated_at": "2026-09-14T12:30:00Z",
        }
    )
    changed_page = adapter.preview_repository(
        selection,
        (changed_record,),
        privacy=_privacy(),
    )
    changed_intake = adapter.intake(selection, changed_record, privacy=_privacy())
    changed_checkpoint, changed_receipt = adapter.import_repository_page(
        checkpoint,
        GitHubRepositoryPage(status=GitHubPageStatus.READY, preview=changed_page),
        (changed_intake,),
        sink,
    )

    assert changed_receipt.outcome is ConnectorOutcome.COMPLETED
    assert changed_receipt.submitted_count == 1
    assert changed_receipt.created_count == 1
    assert changed_checkpoint.committed_delivery_ids == checkpoint.committed_delivery_ids
    assert changed_checkpoint.committed_revision_identities == (
        changed_intake.key.revision_identity(),
    )


def test_github_repository_import_revalidates_legacy_checkpoint_without_revisions(
    tmp_path: Path,
) -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    record = adapter.issue_from_rest(
        {
            "number": 1,
            "html_url": "https://github.com/fixture/project/issues/1",
            "title": "One",
            "body": "Synthetic legacy checkpoint issue body.",
            "updated_at": "2026-09-14T12:00:00Z",
        }
    )
    page = adapter.preview_repository(selection, (record,), privacy=_privacy())
    intake = adapter.intake(selection, record, privacy=_privacy())
    legacy = GitHubRepositoryCheckpoint(
        schema_version=1,
        selection=selection,
        next_cursor=None,
        committed_delivery_ids=(intake.key.delivery_id(),),
    )

    checkpoint, receipt = adapter.import_repository_page(
        legacy,
        GitHubRepositoryPage(status=GitHubPageStatus.READY, preview=page),
        (intake,),
        _capture_sink(tmp_path),
    )

    assert receipt.outcome is ConnectorOutcome.COMPLETED
    assert receipt.submitted_count == 1
    assert checkpoint.committed_revision_identities == (
        intake.key.revision_identity(),
    )


def test_github_repository_import_requires_intake_to_match_acknowledged_preview(
    tmp_path: Path,
) -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    page = adapter.repository_page_from_rest(
        selection,
        (
            {
                "number": 1,
                "html_url": "https://github.com/fixture/project/issues/1",
                "title": "One",
                "body": "Synthetic first issue body.",
                "updated_at": "2026-09-14T12:00:00Z",
            },
        ),
        privacy=_privacy(),
    )
    mismatched = adapter.intake(
        selection,
        adapter.issue_from_rest(
            {
                "number": 2,
                "html_url": "https://github.com/fixture/project/issues/2",
                "title": "Two",
                "body": "Synthetic second issue body.",
                "updated_at": "2026-09-14T12:00:00Z",
            }
        ),
        privacy=_privacy(),
    )

    with pytest.raises(ConnectorContractError, match="invalid github import"):
        adapter.import_repository_page(
            GitHubRepositoryCheckpoint.initial(selection),
            page,
            (mismatched,),
            _capture_sink(tmp_path),
        )


def test_github_repository_import_rejects_partially_committed_page(
    tmp_path: Path,
) -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    records = (
        adapter.issue_from_rest(
            {
                "number": 1,
                "html_url": "https://github.com/fixture/project/issues/1",
                "title": "One",
                "body": "Synthetic first issue body.",
                "updated_at": "2026-09-14T12:00:00Z",
            }
        ),
        adapter.issue_from_rest(
            {
                "number": 2,
                "html_url": "https://github.com/fixture/project/issues/2",
                "title": "Two",
                "body": "Synthetic second issue body.",
                "updated_at": "2026-09-14T12:05:00Z",
            }
        ),
    )
    page = adapter.repository_page_from_rest(
        selection,
        (
            {
                "number": 1,
                "html_url": "https://github.com/fixture/project/issues/1",
                "title": "One",
                "body": "Synthetic first issue body.",
                "updated_at": "2026-09-14T12:00:00Z",
            },
            {
                "number": 2,
                "html_url": "https://github.com/fixture/project/issues/2",
                "title": "Two",
                "body": "Synthetic second issue body.",
                "updated_at": "2026-09-14T12:05:00Z",
            },
        ),
        privacy=_privacy(),
    )
    intakes = tuple(adapter.intake(selection, record, privacy=_privacy()) for record in records)
    partial = GitHubRepositoryCheckpoint(
        schema_version=1,
        selection=selection,
        next_cursor=None,
        committed_delivery_ids=(intakes[0].key.delivery_id(),),
    )

    with pytest.raises(ConnectorContractError, match="invalid github checkpoint"):
        adapter.import_repository_page(partial, page, intakes, _capture_sink(tmp_path))


def test_github_checkpoint_refuses_partial_page_ack_to_protect_crash_recovery() -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    page = adapter.preview_repository(
        selection,
        (
            adapter.issue_from_rest(
                {
                    "number": 1,
                    "html_url": "https://github.com/fixture/project/issues/1",
                    "title": "One",
                    "body": "Synthetic first body.",
                    "updated_at": "2026-09-14T12:00:00Z",
                }
            ),
            adapter.issue_from_rest(
                {
                    "number": 2,
                    "html_url": "https://github.com/fixture/project/issues/2",
                    "title": "Two",
                    "body": "Synthetic second body.",
                    "updated_at": "2026-09-14T12:01:00Z",
                }
            ),
        ),
        privacy=_privacy(),
    )

    with pytest.raises(ConnectorContractError, match="invalid github checkpoint"):
        GitHubRepositoryCheckpoint.initial(selection).advance(
            page,
            committed_delivery_ids=(page.records[0].delivery_id,),
        )


def test_github_page_statuses_model_rate_limit_and_revocation_without_preview() -> None:
    adapter = GitHubSourceAdapter()
    rate_limited = adapter.rate_limited_page(retry_after_seconds=60)
    needs_sign_in = adapter.needs_sign_in_page()

    assert rate_limited.status is GitHubPageStatus.RATE_LIMITED
    assert rate_limited.retry_after_seconds == 60
    assert needs_sign_in.status is GitHubPageStatus.NEEDS_SIGN_IN
    assert needs_sign_in.preview is None
    with pytest.raises(ConnectorContractError, match="invalid github page"):
        GitHubRepositoryPage(status=GitHubPageStatus.RATE_LIMITED, retry_after_seconds=0)


def test_github_repository_listing_is_bounded_metadata_only_and_statused() -> None:
    adapter = GitHubSourceAdapter()
    page = adapter.repository_list_page(
        (
            {
                "owner": {"login": "fixture-owner"},
                "name": "project",
                "html_url": "https://github.com/fixture-owner/project",
                "private": True,
                "description": "Synthetic private description must stay out of listing.",
            },
        ),
        next_cursor="cursor:repositories2",
    )
    encoded = page.to_dict()

    assert page.status is GitHubPageStatus.READY
    assert encoded["next_cursor"] == "cursor:repositories2"
    assert encoded["repositories"] == [
        {
            "html_url": "https://github.com/fixture-owner/project",
            "name": "project",
            "owner": "fixture-owner",
            "selected": False,
        }
    ]
    assert "Synthetic private description" not in repr(encoded)
    assert adapter.repository_list_rate_limited_page(
        retry_after_seconds=30
    ).status is GitHubPageStatus.RATE_LIMITED
    assert adapter.repository_list_needs_sign_in_page().status is GitHubPageStatus.NEEDS_SIGN_IN
    with pytest.raises(ConnectorContractError, match="invalid github repository list"):
        adapter.repository_list_page(
            (
                {
                    "owner": {"login": "fixture-owner"},
                    "name": "project",
                    "html_url": "https://evil.example/fixture-owner/project",
                },
            )
        )


def test_github_adapter_derives_revision_sensitive_capture_intake() -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    first = adapter.issue_from_rest(
        {
            "number": 1,
            "html_url": "https://github.com/fixture/project/issues/1",
            "title": "Original",
            "body": "Synthetic original issue body.",
            "updated_at": "2026-09-14T12:00:00Z",
        }
    )
    changed = adapter.issue_from_rest(
        {
            "number": 1,
            "html_url": "https://github.com/fixture/project/issues/1",
            "title": "Original",
            "body": "Synthetic updated issue body.",
            "updated_at": "2026-09-14T12:30:00Z",
        }
    )

    first_intake = adapter.intake(selection, first, privacy=_privacy())
    changed_intake = adapter.intake(selection, changed, privacy=_privacy())

    assert first.external_id == changed.external_id == "issue:1"
    assert first_intake.key.delivery_id() == changed_intake.key.delivery_id()
    assert first_intake.key.revision_identity() != changed_intake.key.revision_identity()
    assert first_intake.capture_kwargs()["source_reference"] == (
        "https://github.com/fixture/project/issues/1"
    )


def test_github_adapter_rejects_records_that_do_not_match_selected_repository() -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    cross_repo = adapter.issue_from_rest(
        {
            "number": 1,
            "html_url": "https://github.com/other/project/issues/1",
            "title": "Wrong repository",
            "body": "Synthetic cross-repository body.",
            "updated_at": "2026-09-14T12:00:00Z",
        }
    )

    with pytest.raises(ConnectorContractError, match="invalid github record"):
        adapter.preview_repository(selection, (cross_repo,), privacy=_privacy())


def test_github_adapter_rejects_comments_that_do_not_match_selected_repository() -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    cross_repo_comment = adapter.comment_from_rest(
        {
            "id": 10,
            "issue_url": "https://api.github.com/repos/fixture/project/issues/1",
            "html_url": "https://github.com/fixture/other/issues/1#issuecomment-10",
            "body": "Synthetic cross-repository comment body.",
            "updated_at": "2026-09-14T12:00:00Z",
        }
    )

    with pytest.raises(ConnectorContractError, match="invalid github record"):
        adapter.preview_repository(selection, (cross_repo_comment,), privacy=_privacy())


@pytest.mark.parametrize(
    "value",
    (
        {"number": 0, "html_url": "https://github.com/o/r/issues/1", "title": "t", "body": "b"},
        {
            "number": 1,
            "html_url": "https://github.com/o/r/issues/1",
            "title": "t",
            "body": "b",
            "updated_at": "yesterday",
        },
    ),
)
def test_github_adapter_rejects_malformed_issue_payloads(value: dict[str, object]) -> None:
    with pytest.raises(ConnectorContractError):
        GitHubSourceAdapter().issue_from_rest(value)


def test_github_adapter_accepts_empty_issue_body_using_title_for_search_text() -> None:
    adapter = GitHubSourceAdapter()
    record = adapter.issue_from_rest(
        {
            "number": 1,
            "html_url": "https://github.com/o/r/issues/1",
            "title": "Metadata only issue",
            "body": None,
            "updated_at": "2026-09-14T12:00:00Z",
        }
    )

    assert record.body == "Metadata only issue"


def test_github_adapter_rejects_cross_connector_selection() -> None:
    adapter = GitHubSourceAdapter()
    selection = adapter.repository_selection(
        connection_id="account:fixture",
        owner="fixture",
        repository="project",
    )
    bad_selection = type(selection)(
        connector_name="gitlab",
        connection_id=selection.connection_id,
        resource_id=selection.resource_id,
        resource_type=selection.resource_type,
    )

    with pytest.raises(ConnectorContractError, match="invalid github selection"):
        adapter.preview_repository(bad_selection, (), privacy=_privacy())


def _capture_sink(tmp_path: Path) -> ConnectorCaptureSink:
    tasks = open_local_engine(compile_single_user_local(tmp_path / f"brain-{uuid4()}"))
    actor_id = f"actor_{uuid4()}"
    context = PublicJobCaptureContext.create(
        profile=tasks.profile,
        actor_id=actor_id,
        role_claim={
            "actor_id": actor_id,
            "capabilities": ["capture.accept"],
            "role_claim_id": f"role_claim_{uuid4()}",
            "role_id": f"role_{uuid4()}",
            "tenant_id": tasks.profile.tenant_id,
        },
    )
    sink = tasks.capture.public_job_sink(context)
    return ConnectorCaptureSink(
        sink,
        ConnectorBudget(ConnectorBudgetLimits(max_submissions=8)),
        ConnectorRunEvidence(),
    )
