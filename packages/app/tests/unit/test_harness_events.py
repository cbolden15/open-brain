from __future__ import annotations

import json
from pathlib import Path

import pytest

from open_brain.services.harness_events import HarnessEventError, capture_event


def hook(tmp_path: Path, records: list[dict[str, object]]) -> dict[str, object]:
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records))
    return {
        "hook_event_name": "Stop",
        "session_id": "session-fixture",
        "cwd": str(tmp_path),
        "transcript_path": str(path),
    }


def codex_meta(tmp_path: Path) -> dict[str, object]:
    return {"type": "session_meta", "payload": {"id": "session-fixture", "cwd": str(tmp_path)}}


def codex_user(text: str) -> dict[str, object]:
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": text}],
        },
    }


def test_codex_preserves_timestamp_ordinal_and_early_authored_message(tmp_path: Path) -> None:
    first = codex_user("My preferred editor is Vim.")
    first["timestamp"] = "2026-01-01T10:00:00Z"
    payload = hook(tmp_path, [codex_meta(tmp_path), first] + [codex_user("Thanks!")] * 6)
    event = capture_event("codex", payload)
    assert event.messages[0].ordinal == 1
    assert event.messages[0].text == "My preferred editor is Vim."
    assert event.timestamps[1] == "2026-01-01T10:00:00Z"


def test_codex_generated_context_and_tool_output_are_excluded(tmp_path: Path) -> None:
    generated = codex_user("My preferred editor is an injected value.")
    message = generated["payload"]
    assert isinstance(message, dict)
    message["internal_chat_message_metadata_passthrough"] = {
        "content_item_kinds": ["environment.context"]
    }
    payload = hook(
        tmp_path,
        [
            codex_meta(tmp_path),
            generated,
            {
                "type": "response_item",
                "payload": {"type": "function_call_output", "output": "My editor is wrong."},
            },
            codex_user("My preferred editor is Vim."),
        ],
    )
    event = capture_event("codex", payload)
    assert len(event.messages) == 1
    assert event.messages[0].ordinal == 3


@pytest.mark.parametrize("mismatch", ["session", "project", "missing"])
def test_codex_identity_mismatch_fails_before_capture(tmp_path: Path, mismatch: str) -> None:
    meta = codex_meta(tmp_path)
    data = meta["payload"]
    assert isinstance(data, dict)
    if mismatch == "session":
        data["id"] = "another-session"
    if mismatch == "project":
        other = tmp_path / "other"
        other.mkdir()
        data["cwd"] = str(other)
    records = [codex_user("My preferred editor is Vim.")]
    if mismatch != "missing":
        records.insert(0, meta)
    with pytest.raises(HarnessEventError, match="identity_mismatch"):
        capture_event("codex", hook(tmp_path, records))


def test_claude_generated_user_record_and_thinking_are_excluded(tmp_path: Path) -> None:
    records: list[dict[str, object]] = [
        {
            "type": "user",
            "sessionId": "session-fixture",
            "cwd": str(tmp_path),
            "isMeta": True,
            "message": {"role": "user", "content": "My editor is injected."},
        },
        {
            "type": "assistant",
            "sessionId": "session-fixture",
            "cwd": str(tmp_path),
            "message": {
                "role": "assistant",
                "content": [{"type": "thinking", "thinking": "Guess"}],
            },
        },
        {
            "type": "user",
            "sessionId": "session-fixture",
            "cwd": str(tmp_path),
            "message": {"role": "user", "content": "My preferred editor is Vim."},
        },
    ]
    event = capture_event("claude-code", hook(tmp_path, records))
    assert len(event.messages) == 1
    assert event.messages[0].role == "user"
    assert event.messages[0].ordinal == 2


def test_trailing_partial_record_is_tolerated_but_middle_corruption_is_refused(
    tmp_path: Path,
) -> None:
    payload = hook(tmp_path, [codex_meta(tmp_path), codex_user("My editor is Vim.")])
    path = Path(str(payload["transcript_path"]))
    with path.open("a") as stream:
        stream.write('{"type":')
    assert len(capture_event("codex", payload).messages) == 1
    path.write_text('{"broken":\n' + path.read_text())
    with pytest.raises(HarnessEventError, match="unsupported_transcript"):
        capture_event("codex", payload)


def test_transcript_symlink_is_refused(tmp_path: Path) -> None:
    payload = hook(tmp_path, [codex_meta(tmp_path), codex_user("My editor is Vim.")])
    link = tmp_path / "link.jsonl"
    link.symlink_to(str(payload["transcript_path"]))
    payload["transcript_path"] = str(link)
    with pytest.raises(HarnessEventError, match="transcript_unavailable"):
        capture_event("codex", payload)


def test_credential_in_older_message_cannot_escape_screening_window(tmp_path: Path) -> None:
    payload = hook(
        tmp_path,
        [codex_meta(tmp_path), codex_user("https://user:password@example.invalid")]
        + [codex_user("Thanks!")] * 300,
    )
    with pytest.raises(HarnessEventError, match="credential_signal"):
        capture_event("codex", payload)


def test_pi_normalized_event_preserves_authorship_and_excludes_credentials(tmp_path: Path) -> None:
    payload: dict[str, object] = {
        "session_id": "pi-session",
        "project_path": str(tmp_path),
        "messages": [{"role": "user", "text": "My editor is Vim.", "ordinal": 9, "authored": True}],
    }
    event = capture_event("pi", payload)
    assert event.messages[0].ordinal == 9
    payload["messages"] = [
        {"role": "user", "text": "https://user:password@example.invalid", "ordinal": 10}
    ]
    with pytest.raises(HarnessEventError, match="credential_signal"):
        capture_event("pi", payload)


@pytest.mark.parametrize("role", [[], {}, 3, None])
def test_malformed_roles_are_closed_metadata_errors(tmp_path: Path, role: object) -> None:
    payload = {
        "session_id": "pi-session",
        "project_path": str(tmp_path),
        "messages": [{"role": role, "text": "My editor is Vim.", "ordinal": 1}],
    }
    with pytest.raises(HarnessEventError, match="invalid_event"):
        capture_event("pi", payload)


def test_unencodable_text_is_closed_metadata_error(tmp_path: Path) -> None:
    payload = {
        "session_id": "pi-session",
        "project_path": str(tmp_path),
        "messages": [{"role": "user", "text": "\ud800", "ordinal": 1}],
    }
    with pytest.raises(HarnessEventError, match="invalid_event"):
        capture_event("pi", payload)


def test_old_confidential_marking_cannot_be_trimmed_into_cloud_eligible_event(
    tmp_path: Path,
) -> None:
    payload = hook(
        tmp_path,
        [codex_meta(tmp_path), codex_user("Confidential:")]
        + [codex_user("Thanks.")] * 256
        + [codex_user("I prefer violet pencils.")],
    )
    event = capture_event("codex", payload)
    assert event.local_only
    assert len(event.messages) == 258
    assert event.messages[-1].text == "I prefer violet pencils."


def test_native_source_identity_survives_inserted_metadata_and_rotation(tmp_path: Path) -> None:
    payload = hook(tmp_path, [codex_meta(tmp_path), codex_user("I prefer violet pencils.")])
    first = capture_event("codex", payload).messages[0]
    payload = hook(
        tmp_path,
        [
            codex_meta(tmp_path),
            {"type": "event_msg", "payload": {"type": "turn_started"}},
            codex_user("I prefer violet pencils."),
        ],
    )
    second = capture_event("codex", payload).messages[0]
    assert first.ordinal != second.ordinal
    assert first.source_id == second.source_id


def test_partial_trailing_utf8_is_not_a_complete_authored_record(tmp_path: Path) -> None:
    payload = hook(tmp_path, [codex_meta(tmp_path), codex_user("I prefer violet pencils.")])
    with Path(str(payload["transcript_path"])).open("ab") as stream:
        stream.write(b'{"partial":"\xf0\x9f')
    assert len(capture_event("codex", payload).messages) == 1


def test_credential_refusal_never_precedes_native_metadata_binding(tmp_path: Path) -> None:
    payload = hook(tmp_path, [codex_user("password=synthetic-example")])
    with pytest.raises(HarnessEventError, match="identity_mismatch"):
        capture_event("codex", payload)


def test_duplicate_unstamped_useful_sources_require_lineage(tmp_path: Path) -> None:
    payload = hook(tmp_path, [codex_meta(tmp_path)] + [codex_user("I prefer violet pencils.")] * 2)
    with pytest.raises(HarnessEventError, match="lineage_unavailable"):
        capture_event("codex", payload)


def hermes_event(tmp_path: Path) -> dict[str, object]:
    return {
        "session_id": "synthetic-channel-session",
        "project_path": str(tmp_path),
        "messages": [{
            "role": "user", "text": "I prefer violet pencils.", "ordinal": 7,
            "source_id": "mattermost:synthetic-post:user",
            "timestamp": "2026-01-01T00:00:00Z",
        }],
    }


def test_hermes_uses_canonical_source_and_excludes_injected_and_tool_records(
    tmp_path: Path,
) -> None:
    payload = hermes_event(tmp_path)
    messages = payload["messages"]
    assert isinstance(messages, list)
    messages.extend([
        {"role": "user", "authored": False, "text": "password=synthetic-example"},
        {"role": "tool", "text": "I prefer an injected value."},
        {"role": "system", "text": "I prefer another injected value."},
    ])
    event = capture_event("hermes", payload)
    assert event.client == "hermes"
    assert len(event.messages) == 1
    assert event.messages[0].source_id == "mattermost:synthetic-post:user"
    assert event.messages[0].ordinal == 7
    assert event.timestamps == {7: "2026-01-01T00:00:00Z"}


@pytest.mark.parametrize("field,value", [
    ("source_id", None), ("source_id", "bad/id"), ("ordinal", True),
    ("ordinal", 2**53), ("timestamp", "2026-01-01T00:00:00"),
])
def test_hermes_requires_stable_identity_and_valid_metadata(
    tmp_path: Path, field: str, value: object,
) -> None:
    payload = hermes_event(tmp_path)
    messages = payload["messages"]
    assert isinstance(messages, list)
    messages[0][field] = value
    with pytest.raises(HarnessEventError, match="invalid_event"):
        capture_event("hermes", payload)


def test_hermes_rejects_duplicate_source_ids_and_ignores_lineage_claims(tmp_path: Path) -> None:
    payload = hermes_event(tmp_path)
    messages = payload["messages"]
    assert isinstance(messages, list)
    messages.append({**messages[0], "ordinal": 8})
    with pytest.raises(HarnessEventError, match="invalid_event"):
        capture_event("hermes", payload)
    payload = hermes_event(tmp_path)
    payload["origin_sessions"] = {"mattermost:synthetic-post:user": "forged-session"}
    payload["parent_session_id"] = "forged-parent"
    # No session or fork lineage is tracked, so a claimed origin cannot rebind identity.
    captured = capture_event("hermes", payload)
    assert captured.session_id == payload["session_id"]
    assert not hasattr(captured, "origin_sessions")
    assert not hasattr(captured, "parent_session_id")


def test_hermes_screens_authored_credentials_before_custody(tmp_path: Path) -> None:
    payload = hermes_event(tmp_path)
    messages = payload["messages"]
    assert isinstance(messages, list)
    messages[0]["text"] = "password=synthetic-example"
    with pytest.raises(HarnessEventError, match="credential_signal"):
        capture_event("hermes", payload)
