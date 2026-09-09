"""Select distributable metadata for the frozen runtime, without installer state."""

from __future__ import annotations

import ast
import re
from collections.abc import Sequence
from pathlib import PurePosixPath
from types import CodeType

from tools.open_brain_dev.release_audit import ABSOLUTE_HOME_RE

_RUNTIME_METADATA = frozenset({"METADATA", "WHEEL", "entry_points.txt", "top_level.txt"})
_LEGAL_NAMES = ("LICENSE", "COPYING", "NOTICE", "AUTHORS", "COPYRIGHT")


def runtime_metadata(entries: Sequence[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """Filter expanded PyInstaller data entries; preserve resource and legal files.

    Installation records describe the build environment, not the frozen application.
    In particular, RECORD and uv cache files can retain references to direct_url.json.
    Select runtime metadata positively instead of removing just one known path leak.
    """
    selected = []
    for entry in entries:
        path = PurePosixPath(entry[0])
        metadata = next(
            (i for i, part in enumerate(path.parts) if part.lower().endswith(".dist-info")), None
        )
        if metadata is None:
            selected.append(entry)
            continue
        if metadata + 1 == len(path.parts):
            continue
        relative = PurePosixPath(*path.parts[metadata + 1 :])
        filename = relative.name.upper()
        legal = relative.parts[0].lower() == "licenses" or any(
            filename == name or filename.startswith((name + ".", name + "-"))
            for name in _LEGAL_NAMES
        )
        if relative.as_posix() in _RUNTIME_METADATA or legal:
            selected.append(entry)
    return selected


def relocated_sysconfig(source: str, module: str) -> CodeType:
    """Compile generated build variables against the frozen interpreter's prefixes.

    This consumes the trusted build interpreter's literal sysconfig data, not an
    artifact under audit. Preserve every key and non-path scalar. Only occurrences
    of its declared installation prefixes become runtime expressions. Unknown home
    paths fail the build instead of being redacted or exempted by the auditor.
    """
    tree = ast.parse(source)
    statements = [
        node
        for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    if len(statements) != 1 or not isinstance(statements[0], ast.Assign):
        raise ValueError("sysconfig data must be one literal assignment")
    assignment = statements[0]
    if (
        len(assignment.targets) != 1
        or not isinstance(assignment.targets[0], ast.Name)
        or assignment.targets[0].id != "build_time_vars"
    ):
        raise ValueError("sysconfig data assignment is unexpected")
    values = ast.literal_eval(assignment.value)
    if not isinstance(values, dict) or not all(
        isinstance(key, str) and isinstance(value, (str, int)) for key, value in values.items()
    ):
        raise ValueError("sysconfig variables must be scalar metadata")
    if any(ABSOLUTE_HOME_RE.search(key.encode()) for key in values):
        raise ValueError("sysconfig has an unexpected home path key")
    prefixes: dict[str, str] = {}
    for key in ("prefix", "exec_prefix"):
        prefix = values.get(key)
        if not isinstance(prefix, str) or not prefix.startswith("/") or prefix == "/":
            raise ValueError("sysconfig installation prefix is invalid")
        prefixes.setdefault(prefix.rstrip("/"), key)
    pattern = re.compile(
        "(?:"
        + "|".join(re.escape(p) for p in sorted(prefixes, key=len, reverse=True))
        + r")(?=$|/|[\s\"'])"
    )

    def expression(value: str | int) -> ast.expr:
        if not isinstance(value, str):
            return ast.Constant(value)
        pieces: list[ast.expr] = []
        cursor = 0
        for match in pattern.finditer(value):
            pieces.append(ast.Constant(value[cursor : match.start()]))
            pieces.append(
                ast.Attribute(
                    ast.Name("_runtime_sys", ast.Load()), prefixes[match.group()], ast.Load()
                )
            )
            cursor = match.end()
        pieces.append(ast.Constant(value[cursor:]))
        for piece in pieces:
            if (
                isinstance(piece, ast.Constant)
                and isinstance(piece.value, str)
                and ABSOLUTE_HOME_RE.search(piece.value.encode())
            ):
                raise ValueError("sysconfig has an unrelocated home path")
        result = pieces[0]
        for piece in pieces[1:]:
            result = ast.BinOp(result, ast.Add(), piece)
        return result

    rewritten = ast.Module(
        body=[
            ast.Import(names=[ast.alias(name="sys", asname="_runtime_sys")]),
            ast.Assign(
                targets=[ast.Name("build_time_vars", ast.Store())],
                value=ast.Dict(
                    keys=[ast.Constant(key) for key in values],
                    values=[expression(value) for value in values.values()],
                ),
            ),
            ast.Delete(targets=[ast.Name("_runtime_sys", ast.Del())]),
        ],
        type_ignores=[],
    )
    return compile(ast.fix_missing_locations(rewritten), module + ".py", "exec", optimize=0)
