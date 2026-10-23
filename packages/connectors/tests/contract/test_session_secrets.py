from __future__ import annotations

import pytest

from open_brain_connectors.runtime.session_secrets import contains_session_secret

# Synthetic values only. Tokens are assembled so no literal resembles a live credential.
_MIXED = "Q7rT2mXz" + "K9pL4vNw" + "B3sD8fGh" + "J6kY1cUe"


@pytest.mark.parametrize(
    "text",
    [
        "commit 9f50bd0c5e1a2b3c4d5e6f708192a3b4c5d6e7f8 merged",
        "sha256 " + "ab12" * 16,
        "capture_65fdf64a-f32b-4c38-98ca-248de7fd2219 landed",
        "transcript at -Users-example-Projects-oss-open-brain-public-worktrees",
        "upload /media/2025/09/IMG_20250914-Client2026-Final-Deck-V2-Compressed.pdf",
        "branch fix/2026-10-23-session-capture-gate-follow-up-work",
        "contact owner@example.com or 555-123-4567",
        "set the API_TOKEN variable in your shell profile",
    ],
)
def test_identifiers_and_personal_details_are_not_secrets(text: str) -> None:
    assert not contains_session_secret(text)


@pytest.mark.parametrize(
    "text",
    [
        "key sk-ant-api03-" + _MIXED,
        "key sk-proj-" + _MIXED,
        "pat ghp_" + _MIXED + "abcd",
        "pat github_pat_" + _MIXED,
        "slack xoxb-" + "1234567890-" + _MIXED,
        "aws AKIA" + "ABCDEFGHIJ234567",
        "google AIza" + _MIXED + "abc",
        "gitlab glpat-" + _MIXED[:20],
        "stripe sk_live_" + _MIXED,
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "https://user:hunter2@example.com/repo.git",
        "token=synthetic-private-value",
        "Authorization: Bearer abcdefgh12345678",
        "bare " + _MIXED + " pasted",
        "hook /api/webhooks/123456789012345678/" + _MIXED[:16] + "-" + _MIXED[16:] + "_x9Z",
    ],
)
def test_credential_shapes_are_secrets(text: str) -> None:
    assert contains_session_secret(text)
