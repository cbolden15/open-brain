from __future__ import annotations

from urllib.parse import quote

from open_brain_legacy._compat.open_brain.integrations.ports import (
    AuditDisposition,
    AuditFindingCode,
    RuntimeAuditRequest,
    RuntimeField,
    RuntimeManifest,
)
from open_brain_legacy.integrations.runtime_audit import RuntimeManifestAudit


def test_runtime_manifest_audit_rejects_empty_forbidden_root_policy() -> None:
    synthetic_root = "/synthetic/runtime"
    result = RuntimeManifestAudit().inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_empty_forbidden_roots",
                executable_ref=f"{synthetic_root}/bin/external-job",
                argument_refs=(f"--config={synthetic_root}/jobs/external.yaml",),
                working_directory_ref=f"{synthetic_root}/work",
                referenced_file_refs=(f"{synthetic_root}/config.toml",),
            ),
            forbidden_roots=(),
            allowed_roots=(synthetic_root,),
        )
    )

    assert result.disposition is AuditDisposition.DENIED
    assert {finding.code for finding in result.findings} == {
        AuditFindingCode.INVALID_MANIFEST,
    }
    assert synthetic_root not in repr(result.to_dict())


def test_runtime_manifest_audit_rejects_unclassifiable_path_bearing_references() -> (
    None
):
    synthetic_root = "/synthetic/runtime"
    result = RuntimeManifestAudit().inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_unclassifiable_paths",
                executable_ref=f"runner {synthetic_root}/bin/external-job",
                argument_refs=(f"--config {synthetic_root}/jobs/external.yaml",),
                working_directory_ref=f"workdir {synthetic_root}/work",
                referenced_file_refs=(f"config {synthetic_root}/config.toml",),
            ),
            forbidden_roots=("/synthetic/old-source",),
            allowed_roots=(synthetic_root,),
        )
    )

    assert result.disposition is AuditDisposition.DENIED
    assert {(finding.field, finding.code) for finding in result.findings} == {
        (RuntimeField.EXECUTABLE, AuditFindingCode.INVALID_MANIFEST),
        (RuntimeField.ARGUMENT, AuditFindingCode.INVALID_MANIFEST),
        (RuntimeField.WORKING_DIRECTORY, AuditFindingCode.INVALID_MANIFEST),
        (RuntimeField.REFERENCED_FILE, AuditFindingCode.INVALID_MANIFEST),
    }
    assert synthetic_root not in repr(result.to_dict())


def test_runtime_manifest_audit_rejects_dot_segment_alias_of_forbidden_root() -> None:
    result = RuntimeManifestAudit().inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_dot_segment_alias",
                executable_ref="/synthetic/./old-source/bin/runner",
                argument_refs=(),
                working_directory_ref="/synthetic/runtime/work",
                referenced_file_refs=(),
            ),
            forbidden_roots=("/synthetic/old-source",),
            allowed_roots=("/synthetic",),
        )
    )

    assert result.disposition is AuditDisposition.DENIED
    assert {(finding.field, finding.code) for finding in result.findings} == {
        (RuntimeField.EXECUTABLE, AuditFindingCode.INVALID_MANIFEST),
    }


def test_runtime_manifest_audit_rejects_separator_alias_of_forbidden_root() -> None:
    result = RuntimeManifestAudit().inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_separator_alias",
                executable_ref="/synthetic//old-source/bin/runner",
                argument_refs=(),
                working_directory_ref="/synthetic/runtime/work",
                referenced_file_refs=(),
            ),
            forbidden_roots=("/synthetic/old-source",),
            allowed_roots=("/synthetic",),
        )
    )

    assert result.disposition is AuditDisposition.DENIED
    assert {(finding.field, finding.code) for finding in result.findings} == {
        (RuntimeField.EXECUTABLE, AuditFindingCode.INVALID_MANIFEST),
    }


def test_runtime_manifest_audit_classifies_encoded_executable_and_argument_paths() -> None:
    result = RuntimeManifestAudit().inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_encoded_paths",
                executable_ref="%2Fsynthetic%2Fold-source%2Fbin%2Frunner",
                argument_refs=("--config=%2Fsynthetic%2Fold-source%2Fjob.yaml",),
                working_directory_ref="/synthetic/runtime/work",
                referenced_file_refs=(),
            ),
            forbidden_roots=("/synthetic/old-source",),
            allowed_roots=("/synthetic/runtime",),
        )
    )

    assert result.disposition is AuditDisposition.DENIED
    assert {(finding.field, finding.code) for finding in result.findings} == {
        (RuntimeField.EXECUTABLE, AuditFindingCode.FORBIDDEN_REFERENCE),
        (RuntimeField.ARGUMENT, AuditFindingCode.FORBIDDEN_REFERENCE),
    }


def test_runtime_manifest_audit_rejects_encoding_that_exceeds_decode_bound() -> None:
    encoded_executable = "/synthetic/old-source/bin/runner"
    for _ in range(4):
        encoded_executable = quote(encoded_executable, safe="")

    result = RuntimeManifestAudit().inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_decode_bound",
                executable_ref=encoded_executable,
                argument_refs=(),
                working_directory_ref="/synthetic/runtime/work",
                referenced_file_refs=(),
            ),
            forbidden_roots=("/synthetic/old-source",),
            allowed_roots=("/synthetic/runtime",),
        )
    )

    assert result.disposition is AuditDisposition.DENIED
    assert {(finding.field, finding.code) for finding in result.findings} == {
        (RuntimeField.EXECUTABLE, AuditFindingCode.INVALID_MANIFEST),
    }


def test_runtime_manifest_audit_compares_overlapping_roots_by_complete_components() -> (
    None
):
    result = RuntimeManifestAudit().inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_overlapping_roots",
                executable_ref="/synthetic/runtime-old-source/bin/runner",
                argument_refs=(),
                working_directory_ref="/synthetic/runtime-old-source/work",
                referenced_file_refs=(),
            ),
            forbidden_roots=("/synthetic/runtime-old",),
            allowed_roots=("/synthetic/runtime", "/synthetic/runtime-old-source"),
        )
    )

    assert result.disposition is AuditDisposition.ALLOWED
    assert result.findings == ()


def test_runtime_manifest_audit_rejects_old_source_refs_without_execution_and_redacts_paths() -> (
    None
):
    old_source = "/synthetic/old-source"
    allowed_root = "/synthetic/runtime"
    audit = RuntimeManifestAudit()

    denied = audit.inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_old_source",
                executable_ref=f"{old_source}/bin/runner",
                argument_refs=("--config", f"{old_source}/jobs/nightly.yaml"),
                working_directory_ref=f"{old_source}/work",
                referenced_file_refs=(f"{allowed_root}/config.toml",),
            ),
            forbidden_roots=(old_source,),
            allowed_roots=(allowed_root,),
        )
    )

    assert denied.disposition is AuditDisposition.DENIED
    assert {(finding.field, finding.code) for finding in denied.findings} == {
        (RuntimeField.EXECUTABLE, AuditFindingCode.FORBIDDEN_REFERENCE),
        (RuntimeField.ARGUMENT, AuditFindingCode.FORBIDDEN_REFERENCE),
        (RuntimeField.WORKING_DIRECTORY, AuditFindingCode.FORBIDDEN_REFERENCE),
    }
    assert old_source not in repr(denied.to_dict())
    assert allowed_root not in repr(denied.to_dict())

    allowed = audit.inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_allowed_external",
                executable_ref=f"{allowed_root}/bin/external-job",
                argument_refs=("--config", f"{allowed_root}/jobs/external.yaml"),
                working_directory_ref=f"{allowed_root}/work",
                referenced_file_refs=(f"{allowed_root}/config.toml",),
            ),
            forbidden_roots=(old_source,),
            allowed_roots=(allowed_root,),
        )
    )

    assert allowed.disposition is AuditDisposition.ALLOWED
    assert allowed.findings == ()

    outside_allowed = audit.inspect(
        RuntimeAuditRequest(
            manifest=RuntimeManifest(
                manifest_id="manifest_outside_allowed",
                executable_ref=f"{allowed_root}/bin/external-job",
                argument_refs=(),
                working_directory_ref="/synthetic/unapproved/work",
                referenced_file_refs=(),
            ),
            forbidden_roots=(old_source,),
            allowed_roots=(allowed_root,),
        )
    )

    assert outside_allowed.disposition is AuditDisposition.DENIED
    assert outside_allowed.findings[0].field is RuntimeField.WORKING_DIRECTORY
    assert outside_allowed.findings[0].code is AuditFindingCode.OUTSIDE_ALLOWED_ROOT
    assert "/synthetic/unapproved/work" not in repr(outside_allowed.to_dict())
