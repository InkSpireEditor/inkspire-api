# -*- coding: utf-8 -*-
"""Paragraphs and their hashes — the two rules the frontend has to match exactly.

The `ink:provenance` section keys a paragraph's runs by a hash of that paragraph's
text, so a writer who edits one paragraph leaves every other paragraph's provenance
alone. Two things therefore have to mean the same in Python and in TypeScript, down to
the byte: where one paragraph ends and the next begins, and what a paragraph hashes to.
`tests/data/paragraphs.json` is the shared answer, and both suites read it.

A paragraph is separated from the next by a run of **two or more line endings**, where a
line ending is `\\n` or `\\r\\n`. A single line ending inside a paragraph is a line break
and keeps the paragraph whole.

The split keeps every separator, so a body reconstructs byte for byte. `separators` is
one longer than `paragraphs`: there is one before the first paragraph and one after the
last, either of which may be empty. That is what carries a body's leading blank lines
and its closing newline without either becoming part of a paragraph's text — and the
closing newline must stay out, because `ink.render` adds one where a section follows the
body, which would otherwise move the last paragraph's hash on the first save.
"""

from __future__ import annotations

import hashlib
import re

#: A run of two or more line endings, or the single one that closes a body. Written as
#: one pattern because both are separators: everything a paragraph is not.
SEPARATOR = re.compile(r"(?:\r?\n){2,}|\r?\n\Z")

#: How many hex characters of the digest a paragraph is keyed by.
HASH_LENGTH = 16


def split_paragraphs(body: str) -> tuple[list[str], list[str]]:
    """`body` as its paragraphs and the separators around them.

    `separators` holds one more entry than `paragraphs`, so
    `separators[0] + paragraphs[0] + separators[1] + …` rebuilds the body exactly. A
    body of nothing but blank lines has no paragraphs and one separator.
    """
    paragraphs: list[str] = []
    separators: list[str] = []
    pending = ""
    position = 0

    for match in SEPARATOR.finditer(body):
        text = body[position : match.start()]
        if text:
            separators.append(pending)
            paragraphs.append(text)
            pending = match.group()
        else:
            pending += match.group()
        position = match.end()

    text = body[position:]
    if text:
        separators.append(pending)
        paragraphs.append(text)
        pending = ""
    separators.append(pending)

    return paragraphs, separators


def join_paragraphs(paragraphs: list[str], separators: list[str]) -> str:
    """The body `split_paragraphs` took apart, exactly as it was.

    Raises `ValueError` where there is not one separator more than there are paragraphs,
    since the result would silently lose or invent text.
    """
    if len(separators) != len(paragraphs) + 1:
        raise ValueError(
            f"{len(paragraphs)} paragraphs need {len(paragraphs) + 1} separators, "
            f"not {len(separators)}"
        )
    parts = [separators[0]]
    for paragraph, separator in zip(paragraphs, separators[1:]):
        parts.append(paragraph)
        parts.append(separator)
    return "".join(parts)


def paragraph_hash(text: str) -> str:
    """The key `text`'s runs are stored under: `blake2b` of its UTF-8 bytes, truncated.

    The same primitive `derive_id` uses for file ids, and truncated to the same length,
    but a separate constant: the two lengths agree today and answer different questions.
    """
    return hashlib.blake2b(text.encode("utf-8")).hexdigest()[:HASH_LENGTH]


def hashes_of(body: str) -> list[str]:
    """One hash per paragraph of `body`, in the order they are written in."""
    paragraphs, _ = split_paragraphs(body)
    return [paragraph_hash(paragraph) for paragraph in paragraphs]
