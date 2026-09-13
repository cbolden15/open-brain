from __future__ import annotations

from dataclasses import dataclass, field

from open_brain_engine.core.models import PrivacyTier

from open_brain_legacy._compat.open_brain.cli._common import ExitCode
from open_brain_legacy.cli.operations import DigestOutputMode, DigestReport, render_digest


@dataclass
class RecordingDigestService:
    calls: list[tuple[PrivacyTier, DigestOutputMode]] = field(default_factory=list)

    def render(self, *, tier: PrivacyTier, output_mode: DigestOutputMode) -> DigestReport:
        self.calls.append((tier, output_mode))
        return DigestReport(
            event_count=3,
            output_mode=output_mode,
            redacted_count=2,
            replayed=True,
            tier=tier,
        )


def test_digest_preserves_requested_tier_and_never_serializes_digest_content() -> None:
    service = RecordingDigestService()

    result = render_digest(
        service=service,
        tier=PrivacyTier.WORK,
        output_mode=DigestOutputMode.JSON,
    )

    assert service.calls == [(PrivacyTier.WORK, DigestOutputMode.JSON)]
    assert result.exit_code is ExitCode.SUCCESS
    assert result.envelope == {
        "command": "digest",
        "event_count": 3,
        "output_mode": "json",
        "redacted_count": 2,
        "replayed": True,
        "status": "rendered",
        "tier": "work",
    }
    assert "content" not in result.to_json()


def test_digest_redacts_service_failures() -> None:
    class FailingDigestService:
        def render(
            self, *, tier: PrivacyTier, output_mode: DigestOutputMode
        ) -> DigestReport:
            raise RuntimeError("synthetic-content /synthetic/path")

    result = render_digest(service=FailingDigestService(), tier=PrivacyTier.WORK)

    assert result.exit_code is ExitCode.FAILURE
    assert result.envelope["error"] == {
        "code": "digest_operation_failed",
        "message": "operation unavailable; details redacted",
        "redacted": True,
    }
    assert "synthetic" not in result.to_json()
