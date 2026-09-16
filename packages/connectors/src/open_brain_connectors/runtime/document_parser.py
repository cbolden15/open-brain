"""Bounded subprocess entry point. Input is one file snapshot, never a path."""

from __future__ import annotations

import io
import json
import logging
import resource
import sys
import xml.etree.ElementTree as ET
from zipfile import ZipFile

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_EXPANDED_BYTES = 32 * 1024 * 1024
MAX_STREAM_BYTES = 4 * 1024 * 1024
MAX_CAPTURE_TEXT = 65_536
REVISION_HEADER_PREFIX = "Document SHA-256: "
MAX_TEXT = MAX_CAPTURE_TEXT - len(REVISION_HEADER_PREFIX) - 64 - 2
MAX_PAGES = 100
MAX_ZIP_ENTRIES = 1024
WALL_SECONDS = 15
MEMORY_BYTES = 512 * 1024 * 1024


class ParseFailure(ValueError):
    """Closed, content-free extraction failure."""


class _NoDTD(ET.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ParseFailure("document_unsupported_xml")


def _bounded_text(parts: list[str]) -> str:
    text = "\n".join(parts).strip()
    if len(text) > MAX_TEXT:
        raise ParseFailure("document_text_too_large")
    if not text:
        raise ParseFailure("document_no_text")
    if "\x00" in text:
        raise ParseFailure("document_invalid")
    return text


def _docx(data: bytes) -> str:
    if data.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        raise ParseFailure("document_encrypted_or_legacy")
    with ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > MAX_ZIP_ENTRIES or len(set(names)) != len(names):
            raise ParseFailure("document_archive_limit")
        if any(entry.flag_bits & 1 for entry in entries):
            raise ParseFailure("document_encrypted")
        if sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES:
            raise ParseFailure("document_archive_limit")
        if "word/document.xml" not in names or "[Content_Types].xml" not in names:
            raise ParseFailure("document_invalid")
        info = archive.getinfo("word/document.xml")
        if info.file_size > MAX_STREAM_BYTES:
            raise ParseFailure("document_archive_limit")
        with archive.open(info) as handle:
            xml = handle.read(MAX_STREAM_BYTES + 1)
        if len(xml) > MAX_STREAM_BYTES:
            raise ParseFailure("document_archive_limit")
    root = ET.fromstring(xml, parser=ET.XMLParser(target=_NoDTD()))
    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    if root.tag != namespace + "document":
        raise ParseFailure("document_unsupported_xml")
    parts: list[str] = []
    size = 0
    # Paragraph iteration preserves main-body/table order. Relationships, embedded
    # files, macros, images and external references are never opened or executed.
    for paragraph in root.iter(namespace + "p"):
        text = "".join(
            (node.text or "") if node.tag == namespace + "t" else
            "\t" if node.tag == namespace + "tab" else
            "\n" if node.tag in {namespace + "br", namespace + "cr"} else ""
            for node in paragraph.iter()
        )
        size += len(text) + 1
        if size > MAX_TEXT + 1:
            raise ParseFailure("document_text_too_large")
        parts.append(text)
    return _bounded_text(parts)


def _pdf(data: bytes) -> str:
    from pypdf import PdfReader, overwrite_configuration

    if not data.startswith(b"%PDF-"):
        raise ParseFailure("document_invalid")
    overwrite_configuration(
        maximum_declared_stream_length=MAX_STREAM_BYTES,
        array_based_stream_maximum_output_length=MAX_STREAM_BYTES,
        zlib_maximum_output_length=MAX_STREAM_BYTES,
        lzw_maximum_output_length=MAX_STREAM_BYTES,
        run_length_maximum_output_length=MAX_STREAM_BYTES,
        image_maximum_buffer_size=MAX_STREAM_BYTES,
        jbig2dec_binary=None,
    )
    reader = PdfReader(io.BytesIO(data), strict=True)
    if reader.is_encrypted:
        raise ParseFailure("document_encrypted")
    if len(reader.pages) > MAX_PAGES:
        raise ParseFailure("document_page_limit")
    parts: list[str] = []
    size = 0
    for page in reader.pages:
        contents = page.get_contents()
        if contents is not None and len(contents.get_data()) > MAX_STREAM_BYTES:
            raise ParseFailure("document_stream_limit")
        text = page.extract_text()
        size += len(text) + 1
        if size > MAX_TEXT + 1:
            raise ParseFailure("document_text_too_large")
        parts.append(text)
    return _bounded_text(parts)


def main() -> None:
    # The hard address-space cap is enforced on Linux. macOS does not enforce
    # RLIMIT_AS reliably; the parent also watches resident memory there.
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (MEMORY_BYTES, MEMORY_BYTES))
    logging.disable(logging.CRITICAL)
    try:
        data = sys.stdin.buffer.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ParseFailure("document_file_too_large")
        kind = sys.argv[1]
        text = _pdf(data) if kind == "text_pdf" else _docx(data)
        result = {"text": text}
    except ParseFailure as error:
        result = {"error": str(error)}
    except Exception:
        # Parser diagnostics may contain document content. Never forward them.
        result = {"error": "document_invalid"}
    sys.stdout.write(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
