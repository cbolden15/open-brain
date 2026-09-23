"""Execute the exact runnable first-use documentation against one native artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

EXPECTED_ARTIFACT_SHA256: Final = "a6974a513827f11e49eff90d45fcb6aa4cee7e2b05b0d34897e030dd2eccf687"
EXPECTED_GRAPHIFY_SHA256: Final = "63f574a5beaa4e658110230b70a6422ed30ed47730044a5f5d0a886027d08854"
EXPECTED_ASSET_SHA256: Final = {
    "main.js": "1cdfd0f0c2e02a178c835dedf1a09ffe51c37ece4f226f3f33eb4b838c990b90",
    "manifest.json": "5c1f9a8b43dcdf63dab172082034d3447f54238f2d4911d820a3bba545f13fc9",
    "styles.css": "43d7accecdd7b1c60ddf713cce955ddb3ce773eca9e5d943955a6966c9374ee9",
}
FIRST_USE_IDS: Final = (
    "first-use-environment",
    "capture-and-import",
    "route-review-publish",
    "workspace-search-and-complete-read",
    "mcp-grant-isolation",
    "agent-setup-lifecycle",
    "obsidian-plugin-lifecycle",
)
DOCTOR_IDS: Final = (
    "doctor-private-data-directory",
    "doctor-foreground-runtime",
    "doctor-base-dependency-closure",
    "doctor-search-index",
)
EXPECTED_EXAMPLE_SHA256: Final = {
    "first-use-environment": "dcee9e65ddda2004f938b4fb7d8c29142cf334441e86933997fd0a746a540e0e",
    "capture-and-import": "38deda7e462191d3821b9585095483b15a77d8d52b743759a256fec37c7edd4d",
    "route-review-publish": "8163294560e11ca41c2537d3205de564b4870374e8608816b46a347fdf3cf7eb",
    "workspace-search-and-complete-read": (
        "1f79b8f3ae45a6a00abf5aa50eca86afc728c4965ccabcc1721a631d9e6fa46a"
    ),
    "mcp-grant-isolation": "adc3c6874b9d1970a4a945645b47783cc0582320dc146556de96fb53f1933b44",
    "agent-setup-lifecycle": "96382a1539b6b18552db4f43627d943bfc6b981dbc3b06980ce9357b48f82018",
    "obsidian-plugin-lifecycle": "f5e6920f2639e4826fd2b969fbcea9ffb5456ae5d178e4f2fd6e4b393af9cf24",
    "doctor-private-data-directory": (
        "66774024a675eee1420ed65869173e9ac1a28690ec21488405d8b940aa5d4c39"
    ),
    "doctor-foreground-runtime": "29fc35c67eb586ef097657c30ecb9fe0dc2dc416a038c965aa1d33209b53c10c",
    "doctor-base-dependency-closure": (
        "cb967ac6a35660be216ae5c09ab2c5aec3c156cb9cc826327787e3d1eaac5f80"
    ),
    "doctor-search-index": "adcd1554d6348fb3d10262818ec484f1d04eabb62935e1200c90247f83186477",
}
AGENT_SETUP_GRANTS: Final = {
    "--allow-capture",
    "--allow-search",
    "--allow-content-read",
    "--allow-history-read",
    "--allow-inbox-read",
    "--allow-organize",
    "--allow-review-read",
    "--allow-review-propose",
    "--allow-review-decide",
}
COMPATIBILITY_COORDINATES: Final = {
    "Core / engine package": "0.1.0 / exactly 0.1.0",
    "Local state schema": "10",
    "Runtime session": "5",
    "Task contract": "t03.v1",
    "Catalog schema": "2",
    "Plugin bridge": "1",
    "Portable metadata": "5",
}
README_MCP_CONFIGS: Final = (
    ("Capture only", ("mcp", "--allow-capture")),
    ("Search only", ("mcp", "--allow-search")),
    ("Capture and search", ("mcp", "--allow-capture", "--allow-search")),
)
REQUIRED_EXAMPLE_SNIPPETS: Final = {
    "first-use-environment": (" catalog ", " init "),
    "capture-and-import": (" capture ", " import ", " inbox list "),
    "route-review-publish": (
        " space create ",
        " inbox route ",
        " review propose ",
        " review show ",
        " review approve ",
    ),
    "workspace-search-and-complete-read": (
        " workspace setup ",
        " workspace status ",
        "--record-type canonical",
        "--record-type source",
        " read ",
        'cmp "$EXPECTED_PROJECTED" "$RECONSTRUCTED"',
    ),
    "mcp-grant-isolation": (
        "mcp_exchange capture-only --allow-capture",
        "mcp_exchange search-only --allow-search",
        "mcp_exchange combined --allow-capture --allow-search",
    ),
    "agent-setup-lifecycle": (
        "--allow-content-read",
        "--allow-history-read",
        "--allow-inbox-read",
        "--allow-organize",
        "--allow-review-read",
        "--allow-review-propose",
        "--allow-review-decide",
        "authorized_tools",
    ),
    "obsidian-plugin-lifecycle": (
        "obsidian-plugin install",
        "obsidian-plugin status",
        "obsidian-plugin remove",
    ),
    "doctor-private-data-directory": ("doctor --check private-data-directory",),
    "doctor-foreground-runtime": ("doctor --check foreground-runtime",),
    "doctor-base-dependency-closure": ("doctor --check base-dependency-closure",),
    "doctor-search-index": ("doctor --check search-index",),
}
_MARKER = re.compile(
    r"<!-- open-brain-example:([a-z0-9-]+) -->\s*\n```sh\n(.*?)\n```",
    re.DOTALL,
)


class DocumentationExampleError(RuntimeError):
    """The documentation or exact candidate did not satisfy the frozen gate."""


@dataclass(frozen=True, slots=True)
class Example:
    identifier: str
    body: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.body.encode()).hexdigest()


def extract_examples(path: Path) -> tuple[Example, ...]:
    """Extract uniquely identified shell fences without altering their text."""
    text = path.read_text(encoding="utf-8")
    examples = tuple(Example(identifier, body) for identifier, body in _MARKER.findall(text))
    identifiers = [example.identifier for example in examples]
    if len(identifiers) != len(set(identifiers)):
        raise DocumentationExampleError(f"duplicate example identifier in {path.name}")
    return examples


def require_examples(examples: tuple[Example, ...], expected: tuple[str, ...]) -> None:
    """Reject missing, added, or reordered guide blocks."""
    actual = tuple(example.identifier for example in examples)
    if actual != expected:
        raise DocumentationExampleError(
            f"example inventory drift: expected {expected}, got {actual}"
        )


def validate_example_contract(examples: tuple[Example, ...]) -> None:
    """Require each identified block to retain its frozen operation coverage."""
    by_identifier = {example.identifier: example.body for example in examples}
    if set(by_identifier) != set(REQUIRED_EXAMPLE_SNIPPETS):
        raise DocumentationExampleError("example contract inventory drift")
    for identifier, snippets in REQUIRED_EXAMPLE_SNIPPETS.items():
        body = by_identifier[identifier]
        if any(snippet not in body for snippet in snippets):
            raise DocumentationExampleError(f"required example coverage drift: {identifier}")
    agent = by_identifier["agent-setup-lifecycle"]
    setup_lines = tuple(
        line for line in agent.splitlines() if line.lstrip().startswith(("PREVIEW=", "APPLIED="))
    )
    if len(setup_lines) != 2:
        raise DocumentationExampleError("agent setup command coverage drift")
    for line in setup_lines:
        grants = re.findall(r"--allow-[a-z-]+", line)
        if len(grants) != len(AGENT_SETUP_GRANTS) or set(grants) != AGENT_SETUP_GRANTS:
            raise DocumentationExampleError("agent grant coverage drift")
    observed_digests = {example.identifier: example.sha256 for example in examples}
    if observed_digests != EXPECTED_EXAMPLE_SHA256:
        raise DocumentationExampleError("reviewed example body digest drift")


def validate_compatibility_matrix(text: str) -> None:
    """Parse named matrix rows and bind each coordinate to its exact value."""
    try:
        table_text = text.split("## Compatibility coordinates", 1)[1].split("## ", 1)[0]
    except IndexError:
        raise DocumentationExampleError("feature compatibility matrix is missing") from None
    rows: dict[str, str] = {}
    for line in table_text.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 2 or cells[0] in {"Coordinate", "---"}:
            continue
        value = cells[1].replace("`", "")
        rows[cells[0]] = value
    if rows != COMPATIBILITY_COORDINATES:
        raise DocumentationExampleError(
            f"feature compatibility matrix drift: expected {COMPATIBILITY_COORDINATES}, got {rows}"
        )


def read_readme_mcp_configs(text: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Extract the three ordered public MCP JSON configurations."""
    pattern = re.compile(
        r"^### (Capture only|Search only|Capture and search)\s*$.*?^```json\s*$\n(.*?)^```\s*$",
        re.MULTILINE | re.DOTALL,
    )
    configs: list[tuple[str, tuple[str, ...]]] = []
    for label, raw in pattern.findall(text):
        try:
            document = json.loads(raw)
            server = document["mcpServers"]["open-brain"]
            if set(server) != {"command", "args"} or server["command"] != "open-brain":
                raise ValueError
            args = server["args"]
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise ValueError
        except KeyError, TypeError, ValueError, json.JSONDecodeError:
            raise DocumentationExampleError(f"invalid README MCP configuration: {label}") from None
        configs.append((label, tuple(args)))
    result = tuple(configs)
    if result != README_MCP_CONFIGS:
        raise DocumentationExampleError(
            f"README MCP configuration drift: expected {README_MCP_CONFIGS}, got {result}"
        )
    return result


def read_guide_mcp_configs(examples: tuple[Example, ...]) -> tuple[tuple[str, ...], ...]:
    """Parse the exact grant arguments exercised by the first-use guide."""
    body = next(example.body for example in examples if example.identifier == "mcp-grant-isolation")
    calls: list[tuple[str, ...]] = []
    for line in body.splitlines():
        if line.startswith("mcp_exchange "):
            calls.append(tuple(shlex.split(line)))
    expected = (
        ("mcp_exchange", "capture-only", "--allow-capture"),
        ("mcp_exchange", "search-only", "--allow-search"),
        ("mcp_exchange", "combined", "--allow-capture", "--allow-search"),
    )
    result = tuple(calls)
    if result != expected:
        raise DocumentationExampleError(f"guide MCP configuration drift: {result}")
    return tuple(("mcp", *call[2:]) for call in result)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _absolute_existing(path: Path, *, directory: bool = False) -> Path:
    if not path.is_absolute():
        raise DocumentationExampleError("gate paths must be absolute")
    resolved = path.resolve(strict=True)
    if directory != resolved.is_dir():
        raise DocumentationExampleError("gate path has the wrong type")
    return resolved


def _stage_candidate(artifact: Path, destination: Path) -> tuple[Path, dict[str, str]]:
    source_prefix = artifact.parent.parent
    helper = source_prefix / "libexec/open-brain-graphify"
    assets = source_prefix / "share/open-brain/obsidian-plugin"
    _absolute_existing(helper)
    _absolute_existing(assets, directory=True)
    staged = destination / "prefix/bin/open-brain"
    staged.parent.mkdir(parents=True)
    shutil.copy2(artifact, staged)
    staged_helper = destination / "prefix/libexec/open-brain-graphify"
    staged_helper.parent.mkdir(parents=True)
    if _sha256(helper) != EXPECTED_GRAPHIFY_SHA256:
        raise DocumentationExampleError("Graphify helper digest mismatch")
    shutil.copy2(helper, staged_helper)
    staged_assets = destination / "prefix/share/open-brain/obsidian-plugin"
    staged_assets.mkdir(parents=True)
    observed: dict[str, str] = {}
    for name, expected in EXPECTED_ASSET_SHA256.items():
        source = assets / name
        digest = _sha256(source)
        if digest != expected:
            raise DocumentationExampleError(f"Obsidian asset digest mismatch: {name}")
        shutil.copy2(source, staged_assets / name)
        observed[name] = digest
    return staged, observed


def validate_reference_docs(root: Path, examples: tuple[Example, ...]) -> None:
    """Bind the command reference and feature matrix to frozen catalog/help coordinates."""
    cli = (root / "docs/cli.md").read_text(encoding="utf-8")
    matrix = (root / "docs/core-v01-features.md").read_text(encoding="utf-8")
    command_tokens = (
        "`init`",
        "`capture`",
        "`import`",
        "`catalog`",
        "`search-page`",
        "`read`",
        "`history list/show`",
        "`relationship list/decide`",
        "`space create/list/rename`",
        "`inbox list/route`",
        "`review propose/list/show/approve/reject/edit-and-approve`",
        "`agent setup`",
        "`workspace`",
        "`obsidian-plugin install/status/remove`",
        "`doctor --check NAME --json`",
        "`mcp`",
    )
    grant_tokens = tuple(
        f"{name}-read" if name in {"content", "history", "inbox", "review"} else name
        for name in ("capture", "search", "content", "history", "inbox", "organize", "review")
    ) + ("review-propose", "review-decide", "workspace-read", "graph-refresh")
    if any(token not in cli for token in command_tokens + grant_tokens):
        raise DocumentationExampleError("CLI command or grant documentation drift")
    doctor_checks = (
        "private-data-directory",
        "foreground-runtime",
        "base-dependency-closure",
        "search-index",
    )
    if any(token not in cli for token in doctor_checks):
        raise DocumentationExampleError("doctor documentation drift")
    validate_compatibility_matrix(matrix)
    readme = read_readme_mcp_configs((root / "README.md").read_text(encoding="utf-8"))
    guide = read_guide_mcp_configs(examples)
    if tuple(args for _, args in readme) != guide:
        raise DocumentationExampleError("README and guide MCP configurations differ")


def execute_examples(
    examples: tuple[Example, ...],
    *,
    cwd: Path,
    environment: dict[str, str],
    timeout: int,
    stdout_log: Path | None = None,
    stderr_log: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute exact extracted bodies and reject command or assertion failures."""
    completion_file = cwd / ".open-brain-example-completion"
    completion_file.unlink(missing_ok=True)
    environment = {**environment, "OPEN_BRAIN_COMPLETION_FILE": os.fspath(completion_file)}
    instrumented: list[str] = []
    for example in examples:
        instrumented.append(example.body)
        instrumented.append(
            "OPEN_BRAIN_BLOCK_STATUS=$?\n"
            'if test "$OPEN_BRAIN_BLOCK_STATUS" -ne 0; then '
            'exit "$OPEN_BRAIN_BLOCK_STATUS"; fi\n'
            f"printf '%s\\n' '{example.identifier}' >> \"$OPEN_BRAIN_COMPLETION_FILE\""
        )
    script = "\n\n".join(instrumented) + "\n"
    process = subprocess.run(
        ("/bin/sh",),
        cwd=cwd,
        env=environment,
        input=script,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if stdout_log is not None:
        stdout_log.write_text(process.stdout, encoding="utf-8")
    if stderr_log is not None:
        stderr_log.write_text(process.stderr, encoding="utf-8")
    if process.returncode != 0:
        raise DocumentationExampleError(
            "documentation examples failed with exit "
            f"{process.returncode}; stdout tail={process.stdout[-1000:]!r}; "
            f"stderr tail={process.stderr[-1000:]!r}"
        )
    completed = (
        tuple(completion_file.read_text(encoding="utf-8").splitlines())
        if completion_file.is_file()
        else ()
    )
    expected = tuple(example.identifier for example in examples)
    if completed != expected:
        raise DocumentationExampleError(
            f"example completion drift: expected {expected}, got {completed}"
        )
    return process


def run_gate(*, artifact: Path, root: Path, output: Path) -> dict[str, object]:
    """Run all exact marked blocks in one isolated, stateful POSIX shell."""
    artifact = _absolute_existing(artifact)
    root = _absolute_existing(root, directory=True)
    if not output.is_absolute():
        raise DocumentationExampleError("output must be absolute")
    output.parent.mkdir(parents=True, exist_ok=True)
    artifact_digest = _sha256(artifact)
    if artifact_digest != EXPECTED_ARTIFACT_SHA256:
        raise DocumentationExampleError("candidate executable digest mismatch")
    first_use = extract_examples(root / "docs/first-use.md")
    doctor = extract_examples(root / "docs/doctor.md")
    require_examples(first_use, FIRST_USE_IDS)
    require_examples(doctor, DOCTOR_IDS)
    examples = first_use + doctor
    validate_example_contract(examples)
    validate_reference_docs(root, examples)
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="t21-documentation-", dir=output.parent) as temporary:
        temporary_root = Path(temporary)
        executable, assets = _stage_candidate(artifact, temporary_root)
        home = temporary_root / "home"
        work = temporary_root / "work"
        home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        environment = {
            "HOME": os.fspath(home),
            "OPEN_BRAIN": os.fspath(executable),
            "PATH": os.defpath,
            "RUN_ROOT": os.fspath(work / "journey"),
            "TMPDIR": os.fspath(temporary_root),
        }
        process = execute_examples(
            examples,
            cwd=work,
            environment=environment,
            timeout=180,
            stdout_log=output.with_suffix(".stdout.log"),
            stderr_log=output.with_suffix(".stderr.log"),
        )
        completed_ids = tuple(
            (work / ".open-brain-example-completion").read_text(encoding="utf-8").splitlines()
        )
        elapsed = time.monotonic() - started
    report: dict[str, object] = {
        "artifact_sha256": artifact_digest,
        "elapsed_seconds": round(elapsed, 3),
        "completed_example_count": len(completed_ids),
        "completed_examples": list(completed_ids),
        "example_count": len(examples),
        "extracted_example_count": len(examples),
        "graphify_sha256": EXPECTED_GRAPHIFY_SHA256,
        "examples": [{"id": example.identifier, "sha256": example.sha256} for example in examples],
        "obsidian_assets": assets,
        "result": "passed",
        "runtime_isolation": {
            "ambient_credentials": False,
            "cwd": "temporary",
            "home": "temporary",
            "path": os.defpath,
            "pythonpath": False,
        },
        "stderr_sha256": hashlib.sha256(process.stderr.encode()).hexdigest(),
        "stdout_sha256": hashlib.sha256(process.stdout.encode()).hexdigest(),
    }
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        report = run_gate(
            artifact=arguments.artifact,
            root=arguments.root,
            output=arguments.output,
        )
    except (DocumentationExampleError, OSError, subprocess.SubprocessError) as error:
        print(f"documentation gate failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
