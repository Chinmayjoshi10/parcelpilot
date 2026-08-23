"""Verbatim span location.

The single mechanism behind two guarantees: policy parameters are validated
against the clause they claim to come from, and every generated citation is
checked to actually appear in the chunk it points at.

The subtlety is whitespace. These PDFs wrap mid-sentence:

    "... May be cancelled. No fee within 30 minutes of booking. After 30
    minutes, charge INR 250 unless a customer agreement explicitly waives ..."

A human quoting that clause writes it as one line. A strict `in` test would
reject the quote, so the validator would fail on *correct* citations -- which is
worse than useless, because the team would then turn it off.

So matching is **whitespace-insensitive but word-exact**: runs of whitespace are
equivalent to each other, and nothing else is normalised. A quote that changes,
drops or reorders a single word still fails, which is the property that matters
-- "no fee within 30 minutes" must not match a document that says "no fee within
60 minutes".

Offsets are returned against the *original* text, so a citation can highlight
the real span in the real chunk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Runs of any whitespace, including the newlines PDF extraction inserts.
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Span:
    start: int
    end: int
    #: The matched text exactly as it appears in the source, line breaks and
    #: all. This is what gets stored, so a citation reproduces the document
    #: rather than a cleaned-up paraphrase of it.
    text: str


def _collapse_with_map(text: str) -> tuple[str, list[int]]:
    """Collapse whitespace runs, keeping a map back to original offsets.

    `offsets[i]` is the index in `text` of the character that produced
    `collapsed[i]`, which is what makes it possible to report a real span after
    matching against a normalised one.
    """
    collapsed: list[str] = []
    offsets: list[int] = []
    in_space = False

    for index, char in enumerate(text):
        if char.isspace():
            if not in_space:
                collapsed.append(" ")
                offsets.append(index)
                in_space = True
            continue
        collapsed.append(char)
        offsets.append(index)
        in_space = False

    return "".join(collapsed), offsets


def find_span(haystack: str, needle: str, *, case_sensitive: bool = True) -> Span | None:
    """Locate `needle` in `haystack`, tolerating whitespace differences only.

    Returns None when the quote is not present. None is a hard failure for the
    caller: a policy parameter whose clause has changed, or a citation the model
    invented.
    """
    if not needle or not needle.strip():
        return None

    flat_hay, offsets = _collapse_with_map(haystack)
    flat_needle = _WS.sub(" ", needle).strip()

    search_hay = flat_hay if case_sensitive else flat_hay.lower()
    search_needle = flat_needle if case_sensitive else flat_needle.lower()

    position = search_hay.find(search_needle)
    if position < 0:
        return None

    start = offsets[position]
    # The last matched character, mapped back, plus one -- not offsets[position +
    # len] which would point at the character *after* the match and could run
    # past the end of the string.
    last = offsets[position + len(flat_needle) - 1]
    end = last + 1
    return Span(start=start, end=end, text=haystack[start:end])


def contains_verbatim(haystack: str, needle: str) -> bool:
    return find_span(haystack, needle) is not None


def find_all_spans(haystack: str, needle: str) -> list[Span]:
    """Every occurrence. Used to detect an ambiguous quote.

    A parameter or citation whose quote appears more than once in a chunk is
    weak evidence: it cannot identify which occurrence was relied on. Callers
    treat that as a warning rather than an error, since a repeated phrase is
    usually still the same rule.
    """
    spans: list[Span] = []
    cursor = 0
    while cursor < len(haystack):
        span = find_span(haystack[cursor:], needle)
        if span is None:
            break
        spans.append(
            Span(
                start=cursor + span.start,
                end=cursor + span.end,
                text=span.text,
            )
        )
        cursor += span.end
    return spans
