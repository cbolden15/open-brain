"""Bounded Markdown helper protocol for accepted Open Brain snapshots."""

import hashlib
import json
import re
import sys
import tempfile
import unicodedata
from pathlib import Path, PurePosixPath

PROTOCOL = "open-brain-graphify-helper-v1"
LIMIT = 16384
GRAPHIFY_VERSION = "0.9.57"
UPSTREAM_COMMIT = "3f82bf7f837a07fb0f7668fbdbd5662801906942"
PATCH_ID = "ob1-graphify-markdown-1"
PATCH_SHA256 = "f7417ee080e1f5050f41b525f4bb0f97910c14abf6531dcf38cbd2ff28da796b"
PARSER_PROFILE = "pyyaml-6.0.3-pure-python"
CAPABILITIES = {
    "component": {
        "graphify_version": GRAPHIFY_VERSION,
        "parser_profile": PARSER_PROFILE,
        "patch_id": PATCH_ID,
        "patch_sha256": PATCH_SHA256,
        "upstream_commit": UPSTREAM_COMMIT,
    },
    "protocol": PROTOCOL,
    "operations": ["extract_markdown"],
    "max_input_bytes": LIMIT,
    "max_output_bytes": LIMIT,
}


class Rejected(ValueError):
    pass


def require(value):
    if not value:
        raise Rejected("request rejected")


def encode(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def request(raw):
    require(isinstance(raw, bytes) and len(raw) <= LIMIT)

    def unique(pairs):
        value = {}
        for key, child in pairs:
            require(key not in value)
            value[key] = child
        return value

    value = json.loads(raw, object_pairs_hook=unique)
    require(encode(value) == raw)
    require(isinstance(value, dict) and set(value) == {"protocol", "operation", "notes"})
    require(value["protocol"] == PROTOCOL and value["operation"] == "extract_markdown")
    require(isinstance(value["notes"], list) and 0 < len(value["notes"]) <= 64)
    ids, paths = set(), set()
    for note in value["notes"]:
        require(isinstance(note, dict) and set(note) == {"id", "path", "body"})
        require(
            isinstance(note["id"], str)
            and re.fullmatch(
                r"page_[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
                note["id"],
            )
        )
        require(note["id"] not in ids and isinstance(note["body"], str))
        path = note["path"]
        require(
            isinstance(path, str)
            and 0 < len(path.encode("utf-8")) <= 1024
            and path.casefold().endswith(".md")
            and not any(unicodedata.category(character) == "Cc" for character in path)
        )
        parts = PurePosixPath(path).parts
        require(
            not path.startswith("/")
            and str(PurePosixPath(path)) == path
            and all(part not in (".", "..") for part in parts)
        )
        normalized_path = unicodedata.normalize("NFC", path).casefold()
        require(normalized_path not in paths)
        ids.add(note["id"])
        paths.add(normalized_path)
    require(not any(other.startswith(path + "/") for path in paths for other in paths))
    return value["notes"]


def extract(notes):
    from frontmatter_contract import parse_frontmatter
    from graphify.extractors.base import _make_id
    from graphify.extractors.markdown import _MD_LINK_INDEX_CACHE, extract_markdown
    from syntax_links import references

    for note in notes:
        parse_frontmatter(note["body"])

    # Whole-request diagnostic avoids resolving any ambiguous basename by upstream order.
    basenames = [
        unicodedata.normalize("NFC", PurePosixPath(note["path"]).name).casefold() for note in notes
    ]
    if len(basenames) != len(set(basenames)):
        return {
            "protocol": PROTOCOL,
            "status": "blocked",
            "pages": [],
            "links": [],
            "diagnostics": [{"code": "ambiguous_snapshot"}],
        }
    with tempfile.TemporaryDirectory(prefix="ob1-graphify-") as temporary:
        root = Path(temporary).resolve()
        selected = {str(root / note["path"]): note["id"] for note in notes}
        hashes = {}
        for note in notes:
            path = root / note["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(note["body"], encoding="utf-8")
            hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        canonical_targets = {}
        for note in notes:
            source_path = root / note["path"]
            for target, _line, wikilink in references(note["body"]):
                if wikilink and target in selected.values():
                    unresolved_path = source_path.parent / f"{target}.md"
                    canonical_targets[(note["id"], _make_id(str(unresolved_path)))] = target
        _MD_LINK_INDEX_CACHE.clear()
        parts = [extract_markdown(root / note["path"], scan_root=root) for note in notes]
        require(all(not part.get("error") for part in parts))
        upstream_pages = [
            node for part in parts for node in part["nodes"] if node.get("node_kind") == "page"
        ]
        require(len(upstream_pages) == len(notes))
        aliases = {node["id"]: selected[node["source_file"]] for node in upstream_pages}
        require(len(aliases) == len(notes))
        links, diagnostics = set(), set()
        for part in parts:
            for edge in part["edges"]:
                if edge.get("relation") != "references":
                    continue
                source = aliases.get(edge["source"])
                # Bind only an exact permanent ID or an existing-file stamp to the
                # selected inventory; Graphify's path-derived ID is not authority.
                target = canonical_targets.get((source, edge.get("target")))
                if target is None:
                    target = selected.get(edge.get("target_file"))
                require(source is not None and selected.get(edge.get("source_file")) == source)
                if target is None:
                    diagnostics.add((source, "unresolved_reference"))
                elif target != source:
                    links.add((source, target))
        require(
            all(
                hashlib.sha256(Path(path).read_bytes()).hexdigest() == expected
                for path, expected in hashes.items()
            )
        )
        return {
            "protocol": PROTOCOL,
            "status": "ok",
            "pages": sorted(selected.values()),
            "links": [
                {"source": source, "target": target, "kind": "explicit_reference"}
                for source, target in sorted(links)
            ],
            "diagnostics": [
                {"source": source, "code": code} for source, code in sorted(diagnostics)
            ],
        }


def deny_network_and_processes(event, _args):
    if event.startswith("socket.") or event in (
        "subprocess.Popen",
        "os.system",
        "os.fork",
        "os.posix_spawn",
    ):
        raise Rejected("unexpected runtime activity")


def main():
    try:
        if sys.argv[1:] == ["--capabilities"]:
            result = CAPABILITIES
        else:
            require(sys.argv[1:] == ["--extract-markdown"])
            notes = request(sys.stdin.buffer.read(LIMIT + 1))
            sys.addaudithook(deny_network_and_processes)
            result = extract(notes)
        output = encode(result)
        require(len(output) <= LIMIT)
        sys.stdout.buffer.write(output)
    except Exception:  # noqa: BLE001 - bounded path-free rejection, never upstream traceback
        sys.stderr.write("helper request rejected\n")
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
