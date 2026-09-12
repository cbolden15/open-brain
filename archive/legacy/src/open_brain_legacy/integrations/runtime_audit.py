"""Pure, redacted inspection of synthetic external-runtime manifests."""

from __future__ import annotations

from urllib.parse import unquote

from open_brain_legacy._compat.open_brain.integrations.ports import (
    AuditDisposition,
    AuditFinding,
    AuditFindingCode,
    RuntimeAuditRequest,
    RuntimeAuditResult,
    RuntimeField,
)


class RuntimeManifestAudit:
    """Classify manifest references without loading, resolving, or executing them."""

    def inspect(self, request: RuntimeAuditRequest) -> RuntimeAuditResult:
        if not isinstance(request, RuntimeAuditRequest):
            raise ValueError("invalid runtime audit request")

        manifest = request.manifest
        findings = tuple(
            finding
            for field, references in (
                (RuntimeField.EXECUTABLE, (manifest.executable_ref,)),
                (RuntimeField.ARGUMENT, manifest.argument_refs),
                (RuntimeField.WORKING_DIRECTORY, (manifest.working_directory_ref,)),
                (RuntimeField.REFERENCED_FILE, manifest.referenced_file_refs),
            )
            if (
                finding := _finding_for(
                    field=field,
                    references=references,
                    forbidden_roots=request.forbidden_roots,
                    allowed_roots=request.allowed_roots,
                )
            )
            is not None
        )
        return RuntimeAuditResult(
            audit_id=manifest.manifest_id,
            disposition=(
                AuditDisposition.DENIED if findings else AuditDisposition.ALLOWED
            ),
            findings=findings,
        )


def _finding_for(
    *,
    field: RuntimeField,
    references: tuple[str, ...],
    forbidden_roots: tuple[str, ...],
    allowed_roots: tuple[str, ...],
) -> AuditFinding | None:
    canonical_forbidden_roots = _canonical_roots(forbidden_roots)
    canonical_allowed_roots = _canonical_roots(allowed_roots)
    if canonical_forbidden_roots is None or canonical_allowed_roots is None:
        return AuditFinding(field=field, code=AuditFindingCode.INVALID_MANIFEST)

    canonical_paths: list[tuple[str, ...]] = []
    for reference in references:
        paths = _paths_for(reference)
        if paths is None or (field is not RuntimeField.ARGUMENT and not paths):
            return AuditFinding(field=field, code=AuditFindingCode.INVALID_MANIFEST)
        canonical_paths.extend(paths)
    if any(
        _matches_root(path, root)
        for path in canonical_paths
        for root in canonical_forbidden_roots
    ):
        return AuditFinding(field=field, code=AuditFindingCode.FORBIDDEN_REFERENCE)
    if any(
        not any(_matches_root(path, root) for root in canonical_allowed_roots)
        for path in canonical_paths
    ):
        return AuditFinding(field=field, code=AuditFindingCode.OUTSIDE_ALLOWED_ROOT)
    return None


def _canonical_roots(roots: tuple[str, ...]) -> tuple[tuple[str, ...], ...] | None:
    if not roots:
        return None
    canonical_roots = tuple(_canonical_path(root) for root in roots)
    if any(root is None for root in canonical_roots):
        return None
    return tuple(root for root in canonical_roots if root is not None)


def _paths_for(reference: str) -> tuple[tuple[str, ...], ...] | None:
    paths: list[tuple[str, ...]] = []
    variants = _decoded_variants(reference)
    if variants is None:
        return None
    for variant in variants:
        path_reference = _path_reference_from_variant(variant)
        if path_reference is None:
            if "/" in variant or "\\" in variant:
                return None
            continue
        path = _canonical_path(path_reference)
        if path is None:
            return None
        paths.append(path)
    if not paths:
        return ()
    if len(set(paths)) != 1:
        return None
    return (paths[0],)


def _path_reference_from_variant(reference: str) -> str | None:
    if reference.startswith("/"):
        return reference
    _, separator, value = reference.partition("=")
    if separator and value.startswith("/"):
        return value
    return None


def _canonical_path(value: str) -> tuple[str, ...] | None:
    if (
        not value.startswith("/")
        or "\\" in value
        or any(character.isspace() for character in value)
        or _has_invalid_percent_escape(value)
    ):
        return None
    components = tuple(value.split("/")[1:])
    if any(component in {"", ".", ".."} for component in components):
        return None
    return components


def _has_invalid_percent_escape(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value) or any(
            character not in "0123456789abcdefABCDEF"
            for character in value[index + 1 : index + 3]
        ):
            return True
        index += 3
    return False


def _decoded_variants(value: str) -> tuple[str, ...] | None:
    variants = [value]
    for _ in range(3):
        decoded = unquote(variants[-1])
        if decoded == variants[-1]:
            return tuple(variants)
        variants.append(decoded)
    return None if unquote(variants[-1]) != variants[-1] else tuple(variants)


def _matches_root(path: tuple[str, ...], root: tuple[str, ...]) -> bool:
    return path[: len(root)] == root
