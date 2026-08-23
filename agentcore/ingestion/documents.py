"""PDF parsing and chunking.

Chunk boundaries are chosen to match how the corpus is actually cited. These
documents are numbered policies ("1. Scope and source precedence", "2. Failed-
pickup service credits"), and a question is always answered by *one clause*. So
we split on section headings rather than a fixed character window.

That choice pays off three times over:

* A citation lands on the operative clause instead of an arbitrary 800-character
  slice that happens to straddle two rules.
* `section_path` becomes a real breadcrumb for the citation inspector.
* The verbatim-span validator has a tight haystack, so a quote that "looks
  right" but came from an adjacent rule fails instead of passing.

A fixed-size window would have been fewer lines and materially worse at the one
thing this system is judged on.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from agentcore.errors import IngestionError
from agentcore.logging import get_logger

log = get_logger(__name__)

#: Matches "1. Heading", "2.3 Heading", "Section 4 - Heading".
_SECTION_RE = re.compile(
    r"^(?:section\s+)?(\d+(?:\.\d+)*)[.)]?\s+(?P<title>[A-Z][^\n]{2,80})\s*$",
    re.IGNORECASE,
)

#: These PDFs use "●" plus a zero-width space for bullets. Left as-is they
#: pollute both the FTS vector and any quoted span, so a quote copied from a
#: bullet would never match the stored text.
_INVISIBLE = dict.fromkeys(
    [ord(c) for c in ("​", "‌", "‍", "﻿", "­")]
)

#: Sections longer than this are split further. Generous, because splitting a
#: clause is worse than a slightly large chunk: retrieval can tolerate extra
#: context, but a citation that spans two rules cannot be trusted.
MAX_CHUNK_CHARS = 2400
#: Below this, a section is merged forward -- a two-line heading fragment on its
#: own retrieves badly and cites nothing useful.
MIN_CHUNK_CHARS = 120


@dataclass(frozen=True)
class ParsedChunk:
    ordinal: int
    page_from: int
    page_to: int
    section_path: str | None
    text: str

    @property
    def token_estimate(self) -> int:
        return max(1, (len(self.text) + 3) // 4)


@dataclass(frozen=True)
class ParsedDocument:
    filename: str
    title: str
    #: Whole-file digest. Makes ingestion idempotent and pins the exact bytes
    #: behind every citation, so a past answer stays reproducible.
    content_sha256: str
    page_count: int
    full_text: str
    chunks: list[ParsedChunk]


def normalise(text: str) -> str:
    """Canonicalise text before it is stored, searched or quoted.

    Applied exactly once, at ingest. Doing it later -- or twice -- means the
    quote the model returns cannot be matched against the stored chunk, which
    would turn the citation validator into a source of false failures.
    """
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_INVISIBLE)
    text = text.replace("•", "-").replace("●", "-").replace("▪", "-")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Collapse runs of spaces/tabs but keep newlines: they carry the structure
    # the section splitter relies on.
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.strip() for line in text.split("\n")]
    # Canonical bullet spacing. Some bullets are followed by a real space in the
    # extracted text and some only by a zero-width space that was just removed,
    # which would leave "-DRAFT" sitting beside "- BOOKED" in the same list.
    # Quoted spans must match byte-for-byte, so the marker is normalised to
    # exactly one trailing space. Anchored at line start, so mid-line hyphens
    # ("P1 - Critical", "return-to-origin") are untouched.
    lines = [re.sub(r"^-(?=\S)", "- ", line) for line in lines]
    return "\n".join(lines).strip()


def parse_pdf(path: Path) -> ParsedDocument:
    """Parse one PDF into normalised, section-aligned chunks."""
    raw_bytes = path.read_bytes()
    digest = hashlib.sha256(raw_bytes).hexdigest()

    try:
        doc = pymupdf.open(path)
    except Exception as exc:  # noqa: BLE001 - any parse failure is one document
        raise IngestionError(f"cannot open {path.name}: {exc}", filename=path.name) from exc

    try:
        pages = [normalise(page.get_text("text")) for page in doc]
        page_count = doc.page_count
    finally:
        doc.close()

    full_text = "\n\n".join(p for p in pages if p)
    if not full_text.strip():
        # A scanned PDF with no text layer. Loud failure: silently indexing an
        # empty document means the answer "no policy covers this" is a lie.
        raise IngestionError(
            f"{path.name} contains no extractable text (is it a scan?)",
            filename=path.name,
        )

    chunks = chunk_pages(pages)
    title = _infer_title(pages[0] if pages else "", path.name)

    log.info(
        "pdf_parsed",
        filename=path.name,
        pages=page_count,
        chars=len(full_text),
        chunks=len(chunks),
    )
    return ParsedDocument(
        filename=path.name,
        title=title,
        content_sha256=digest,
        page_count=page_count,
        full_text=full_text,
        chunks=chunks,
    )


def chunk_pages(pages: list[str]) -> list[ParsedChunk]:
    """Split pages into section-aligned chunks, tracking page provenance."""
    sections: list[tuple[str | None, int, list[str]]] = []
    current_heading: str | None = None
    current_page = 1
    buffer: list[str] = []

    def flush() -> None:
        if buffer and any(line.strip() for line in buffer):
            sections.append((current_heading, current_page, list(buffer)))
        buffer.clear()

    for page_number, page_text in enumerate(pages, start=1):
        for line in page_text.split("\n"):
            match = _SECTION_RE.match(line.strip())
            if match:
                flush()
                current_heading = f"{match.group(1)}. {match.group('title').strip()}"
                current_page = page_number
                buffer.append(line.strip())
            else:
                if not buffer:
                    current_page = page_number
                buffer.append(line)
    flush()

    # Preamble before the first numbered heading (title, status, effective
    # date) is its own section. It matters: "Status: DEPRECATED" and
    # "Effective: 1 May 2026" live there and drive classification.
    merged = _merge_small(sections)

    chunks: list[ParsedChunk] = []
    for heading, page, lines in merged:
        text = "\n".join(lines).strip()
        if not text:
            continue
        for part in _split_oversized(text):
            chunks.append(
                ParsedChunk(
                    ordinal=len(chunks),
                    page_from=page,
                    page_to=page,
                    section_path=heading,
                    text=part,
                )
            )
    return chunks


def _merge_small(
    sections: list[tuple[str | None, int, list[str]]],
) -> list[tuple[str | None, int, list[str]]]:
    """Fold undersized sections into the next one.

    A heading whose body did not survive extraction retrieves noise and cites
    nothing, so it is attached to the section it introduces.
    """
    merged: list[tuple[str | None, int, list[str]]] = []
    carry: list[str] = []
    carry_heading: str | None = None
    carry_page = 1

    for heading, page, lines in sections:
        body = "\n".join(lines).strip()
        if len(body) < MIN_CHUNK_CHARS:
            if not carry:
                carry_heading, carry_page = heading, page
            carry.extend(lines)
            continue
        if carry:
            merged.append((carry_heading or heading, carry_page, carry + lines))
            carry, carry_heading = [], None
        else:
            merged.append((heading, page, lines))

    if carry:
        if merged:
            last_heading, last_page, last_lines = merged[-1]
            merged[-1] = (last_heading, last_page, last_lines + carry)
        else:
            merged.append((carry_heading, carry_page, carry))
    return merged


def _split_oversized(text: str) -> list[str]:
    """Split an over-long section on paragraph, then line, boundaries.

    Never mid-sentence: a chunk that begins half way through a rule produces
    citations that read as though the qualifying clause does not exist.
    """
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    parts: list[str] = []
    current = ""
    for block in re.split(r"\n\s*\n", text):
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= MAX_CHUNK_CHARS:
            current = candidate
            continue
        if current:
            parts.append(current)
        if len(block) <= MAX_CHUNK_CHARS:
            current = block
            continue
        # A single paragraph over the limit: fall back to line boundaries.
        current = ""
        for line in block.split("\n"):
            candidate = f"{current}\n{line}" if current else line
            if len(candidate) <= MAX_CHUNK_CHARS:
                current = candidate
            else:
                if current:
                    parts.append(current)
                current = line
    if current:
        parts.append(current)
    return parts


def _infer_title(first_page: str, fallback: str) -> str:
    """Take the document's own title line, not the filename.

    Filenames are an operator convention; the title is what a user recognises
    in a citation chip.
    """
    for line in first_page.split("\n"):
        stripped = line.strip()
        if len(stripped) > 8 and not stripped.lower().startswith(
            ("status:", "effective:", "account:", "customer:", "updated:", "term:")
        ):
            return stripped[:200]
    return Path(fallback).stem.replace("_", " ")
