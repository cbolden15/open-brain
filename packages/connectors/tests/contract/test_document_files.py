from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from open_brain_engine.engine import open_local_engine
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from open_brain.profile import compile_single_user_local
from open_brain_connectors.runtime import document_files, document_parser
from open_brain_connectors.runtime.connectors import ConnectorContractError
from open_brain_connectors.runtime.document_files import extract_selected_document
from open_brain_connectors.runtime.document_import import document_preview, import_document
from open_brain_connectors.runtime.source_cli import run_cli

CONNECTION = "account:document-fixture"


def _docx(path: Path, text: str, *, xml: bytes | None = None) -> None:
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", xml or (
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + escape(text) + '</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r>'
            '<w:t>Table café fixture</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'
            '</w:body></w:document>'
        ).encode())


def _pdf(path: Path, text: str = "Synthetic PDF needle", *, encrypted: bool = False) -> None:
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 10 100 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("synthetic-fixture")
    writer.write(path)


def _selected(path: Path) -> document_files.SelectedDocument:
    return extract_selected_document(path, title="Fixture document", connection_id=CONNECTION)


@pytest.mark.parametrize("kind", ["pdf", "docx"])
def test_real_file_preview_import_replay_revision_and_portable_history(
    tmp_path: Path, kind: str,
) -> None:
    source = tmp_path / f"private-filename.{kind}"
    write = _pdf if kind == "pdf" else _docx
    write(source, "Originalneedle fixture")
    original = _selected(source)
    preview = document_preview(original, CONNECTION)
    assert "Originalneedle" in str(preview["extracted_text"])
    if kind == "docx":
        assert "Table café fixture" in str(preview["extracted_text"])
    assert str(tmp_path) not in json.dumps(preview)
    assert "private-filename" not in json.dumps(preview)
    brain = tmp_path / "brain"
    profile = compile_single_user_local(brain)

    def capture(document: document_files.SelectedDocument) -> dict[str, object]:
        return import_document(
            document, connection_id=CONNECTION, preview_id=document.preview_id, brain_root=brain,
        )

    first = capture(original)
    assert first["status"] == "imported"
    replay = capture(_selected(source))
    assert replay["status"] == "unchanged"
    assert replay["capture_id"] == first["capture_id"]
    tasks = open_local_engine(profile)
    results = tasks.retrieval.search("Originalneedle")
    assert len(results) == 1
    assert results[0].trust == "third_party"
    assert results[0].provenance.source_origin == "third_party"
    write(source, "Revisedneedle fixture")
    changed = _selected(source)
    assert changed.record.document_id == original.record.document_id
    assert changed.record.revision_id != original.record.revision_id
    with pytest.raises(ConnectorContractError, match="document_preview_changed"):
        import_document(
            changed, connection_id=CONNECTION, preview_id=original.preview_id, brain_root=brain,
        )
    second = capture(changed)
    assert second["capture_id"] != first["capture_id"]
    reopened = open_local_engine(compile_single_user_local(brain))
    assert not reopened.retrieval.search("Originalneedle")
    assert len(reopened.retrieval.search("Revisedneedle")) == 1
    assert capture(changed)["status"] == "unchanged"
    export = tmp_path / "export"
    reopened.portability.export(export, export_id=f"export_{uuid4()}")
    exported = b"\n".join(p.read_bytes() for p in export.rglob("*") if p.is_file())
    assert b"Originalneedle" in exported and b"Revisedneedle" in exported
    assert original.record.revision_id.encode() in exported
    assert changed.record.revision_id.encode() in exported
    assert b"private-filename" not in exported
    assert str(tmp_path).encode() not in exported


def test_cli_preview_then_import_and_fresh_process_retrieval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "fixture.docx"
    _docx(source, "Freshprocessneedle")
    args = ["--file", str(source), "--title", "Fixture"]
    assert run_cli(["local-document", "preview", *args]) == 0
    preview = json.loads(capsys.readouterr().out)
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    assert run_cli([
        "local-document", "import", *args, "--preview-id", preview["preview_id"],
        "--brain-root", str(brain),
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "imported"
    assert "Freshprocessneedle" not in json.dumps(result)
    query = subprocess.run([
        sys.executable, "-I", "-c",
        "from pathlib import Path; from open_brain.profile import compile_single_user_local; "
        "from open_brain_engine.engine import open_local_engine; import sys; "
        "r=open_local_engine(compile_single_user_local(Path(sys.argv[1])))"
        ".retrieval.search('Freshprocessneedle'); print(len(r), r[0].trust)", str(brain),
    ], capture_output=True, text=True, timeout=20, check=True)
    assert query.stdout.strip() == "1 third_party"


@pytest.mark.parametrize("case,code", [
    ("encrypted", "document_encrypted"), ("blank", "document_no_text"),
    ("malformed", "document_invalid"), ("legacy", "document_encrypted_or_legacy"),
    ("dtd", "document_unsupported_xml"), ("long", "document_text_too_large"),
])
def test_parser_rejects_unsupported_documents(tmp_path: Path, case: str, code: str) -> None:
    path = tmp_path / ("fixture.pdf" if case in {"encrypted", "blank", "malformed"}
                       else "fixture.docx")
    if case == "encrypted":
        _pdf(path, encrypted=True)
    elif case == "blank":
        _pdf(path, text="")
    elif case == "malformed":
        path.write_bytes(b"%PDF-1.7\nprivate malformed content")
    elif case == "legacy":
        path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1"))
    elif case == "dtd":
        _docx(path, "", xml=(
            '<?xml version="1.0" encoding="utf-16"?>'
            '<!DOCTYPE document [<!ENTITY attack "PRIVATE">]><document>&attack;</document>'
        ).encode("utf-16"))
    else:
        _docx(path, "x" * (document_parser.MAX_TEXT + 1))
    with pytest.raises(ConnectorContractError, match=code):
        _selected(path)


def test_pdf_page_limit_and_docx_expansion_limit(tmp_path: Path) -> None:
    pdf = tmp_path / "pages.pdf"
    writer = PdfWriter()
    for _ in range(document_parser.MAX_PAGES + 1):
        writer.add_blank_page(width=300, height=300)
    writer.write(pdf)
    with pytest.raises(ConnectorContractError, match="document_page_limit"):
        _selected(pdf)
    docx = tmp_path / "large.docx"
    _docx(docx, "fixture")
    with ZipFile(docx, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr("ignored.bin", b"0" * (document_parser.MAX_EXPANDED_BYTES + 1))
    with pytest.raises(ConnectorContractError, match="document_archive_limit"):
        _selected(docx)


def test_extracted_text_boundary_reserves_capture_header(tmp_path: Path) -> None:
    path = tmp_path / "boundary.docx"
    brain = tmp_path / "brain"
    compile_single_user_local(brain)
    assert document_parser.MAX_TEXT == 65_452
    assert document_parser.MAX_CAPTURE_TEXT == 65_536
    text = "a " * (document_parser.MAX_TEXT // 2 - 1) + "ok"

    def write(value: str) -> None:
        _docx(path, "", xml=(
            '<w:document xmlns:w="http://schemas.openxmlformats.org/'
            'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'
            + value + '</w:t></w:r></w:p></w:body></w:document>'
        ).encode())

    write(text)
    document = _selected(path)
    assert len(str(document_preview(document, CONNECTION)["extracted_text"])) == len(text)
    assert import_document(document, connection_id=CONNECTION, preview_id=document.preview_id,
                           brain_root=brain)["status"] == "imported"
    write(text + "x")
    with pytest.raises(ConnectorContractError, match="document_text_too_large"):
        _selected(path)


@pytest.mark.parametrize("case", ["symlink", "hardlink", "fifo", "oversize", "missing"])
def test_selected_file_rejects_unsafe_objects(tmp_path: Path, case: str) -> None:
    path = tmp_path / "selected.pdf"
    target = tmp_path / "target.pdf"
    _pdf(target)
    if case == "symlink":
        path.symlink_to(target)
    elif case == "hardlink":
        os.link(target, path)
    elif case == "fifo":
        os.mkfifo(path)
    elif case == "oversize":
        with path.open("wb") as handle:
            handle.truncate(document_parser.MAX_FILE_BYTES + 1)
    with pytest.raises(ConnectorContractError, match="document_"):
        _selected(path)


def test_parser_timeout_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    popen = subprocess.Popen
    children: list[subprocess.Popen[bytes]] = []

    def sleeping_child(*args: Any, **kwargs: Any) -> subprocess.Popen[bytes]:
        child = popen([sys.executable, "-c", "import time; time.sleep(30)"], **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", sleeping_child)
    monkeypatch.setattr(document_files, "WALL_SECONDS", 0.05)
    with pytest.raises(ConnectorContractError, match="document_parser_timeout"):
        document_files._extract(b"fixture", "text_pdf")
    assert children and all(child.poll() is not None for child in children)


def test_stale_title_connection_and_invalid_preview_rejected_before_brain_write(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fixture.docx"
    _docx(source, "fixture")
    selected = _selected(source)
    changed = extract_selected_document(source, title="Changed", connection_id=CONNECTION)
    assert changed.preview_id != selected.preview_id
    for token in [selected.preview_id, "☃", ""]:
        with pytest.raises(ConnectorContractError, match="document_preview_changed"):
            import_document(changed, connection_id=CONNECTION, preview_id=token,
                            brain_root=tmp_path / "uncreated")
    assert not (tmp_path / "uncreated").exists()
    with pytest.raises(ConnectorContractError, match="document_preview_changed"):
        import_document(selected, connection_id="account:other", preview_id=selected.preview_id,
                        brain_root=tmp_path / "uncreated")
    assert not (tmp_path / "uncreated").exists()


def test_file_replacement_during_read_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "fixture.docx"
    _docx(path, "Original")
    original_fstat = os.fstat
    calls = 0

    def replace_on_second_stat(fd: int) -> os.stat_result:
        nonlocal calls
        if not stat.S_ISREG(original_fstat(fd).st_mode):
            return original_fstat(fd)
        calls += 1
        if calls == 2:
            replacement = tmp_path / "replacement.docx"
            _docx(replacement, "Replacement")
            replacement.replace(path)
        return original_fstat(fd)

    monkeypatch.setattr(os, "fstat", replace_on_second_stat)
    with pytest.raises(ConnectorContractError, match="document_file_changed"):
        _selected(path)


def test_redaction_failure_is_checked_before_capture(tmp_path: Path) -> None:
    path = tmp_path / "fixture.docx"
    # Assemble a synthetic detector canary without committing a credential-shaped literal.
    _docx(path, "api" + "_key=" + "synthetic-fixture-value")
    with pytest.raises(ConnectorContractError, match="invalid local document record"):
        _selected(path)


def test_pdf_parser_disables_external_decoder_and_bounds_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import pypdf

    path = tmp_path / "fixture.pdf"
    _pdf(path)
    reader = pypdf.PdfReader
    settings: list[object] = []

    def checked_reader(*args: Any, **kwargs: Any) -> pypdf.PdfReader:
        configuration = pypdf.get_configuration()
        assert configuration.jbig2dec_binary is None
        assert configuration.zlib_maximum_output_length == document_parser.MAX_STREAM_BYTES
        assert configuration.lzw_maximum_output_length == document_parser.MAX_STREAM_BYTES
        settings.append(configuration)
        return reader(*args, **kwargs)

    monkeypatch.setattr(pypdf, "PdfReader", checked_reader)
    with pypdf.apply_configuration(jbig2dec_binary="/synthetic/must-not-execute"):
        assert "Synthetic PDF needle" in document_parser._pdf(path.read_bytes())
    assert len(settings) == 1


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin resident-memory watchdog")
def test_mac_parser_memory_guard_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(document_files, "MEMORY_BYTES", 1)
    with pytest.raises(ConnectorContractError, match="document_parser_memory_limit"):
        document_files._extract(b"%PDF-1.7\n", "text_pdf")


def test_cli_errors_do_not_expose_document_or_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / "private-title.pdf"
    source.write_bytes(b"malformed PRIVATE CONTENT")
    assert run_cli(["local-document", "preview", "--file", str(source),
                    "--title", "Fixture"]) == 78
    result = capsys.readouterr()
    assert json.loads(result.out)["error"]["code"] == "document_invalid"
    assert not result.err
    assert str(tmp_path) not in result.out and "PRIVATE" not in result.out
