from __future__ import annotations

from open_brain_legacy.cli.status import show_status
from open_brain_legacy.operations.status import (
    StatusMetric,
    StatusReading,
    StatusUnavailableClass,
    collect_status,
)


def test_status_serializes_metadata_only_and_preserves_strict_exit() -> None:
    canary = "content from /synthetic/private"

    def failed(timeout_seconds: float) -> StatusReading:
        raise RuntimeError(canary)

    status = collect_status(
        probes={
            StatusMetric.CAPTURES_TODAY: lambda timeout: StatusReading.available(value=2),
            StatusMetric.OPEN_REVIEWS: lambda timeout: StatusReading.unavailable(
                StatusUnavailableClass.NOT_CONFIGURED
            ),
            StatusMetric.FAILED_JOBS: failed,
        },
        timeout_seconds=1.0,
        strict=True,
    )

    result = show_status(result=status)

    assert result.exit_code == 78
    assert result.envelope["command"] == "status"
    assert result.envelope["status"] == "partial"
    assert result.envelope["metrics"] == status.to_dict()["metrics"]
    assert canary not in result.to_json()
    assert '"path"' not in result.to_json()
    assert '"content"' not in result.to_json()

