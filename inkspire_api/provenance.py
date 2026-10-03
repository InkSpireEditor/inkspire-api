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

`reconcile` is the other half of this module: a paragraph edited outside the editor no
longer hashes to its stored key, and its runs describe prose that is no longer there.
Recomputing the hash is the wrong fix — it records the new text while keeping offsets
measured against the old, which replaces a detectable problem with a silent wrong
answer. So the runs are diffed forward instead. Finding the old text to diff from needs
git, which lives in `repository.py`; everything here is pure.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
from collections.abc import Sequence

import yaml

logger = logging.getLogger(__name__)

#: A run of two or more line endings, or the single one that closes a body. Written as
#: one pattern because both are separators: everything a paragraph is not.
SEPARATOR = re.compile(r"(?:\r?\n){2,}|\r?\n\Z")

#: How many hex characters of the digest a paragraph is keyed by.
HASH_LENGTH = 16

#: Written by hand. The default, and the only kind that is never stored.
USER = "user"

#: Written by a model.
GEN = "gen"

#: Written by a model and then corrected by hand.
FIX = "fix"

#: One stretch of one kind: `(start, end, kind)`, offsets relative to the paragraph and
#: `end` exclusive. A sequence of three is accepted anywhere one is read, so the lists
#: a YAML section parses to need no conversion first.
Run = tuple[int, int, str]

#: A paragraph's hash to its runs. `user` runs are absent, so a paragraph with no
#: model-written text has an empty list.
Metadata = dict[str, list[Run]]


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


# --- runs, expanded and collapsed ------------------------------------------


def expand_runs(runs: Sequence[Sequence], length: int) -> list[str]:
    """`runs` as one kind per character, over a paragraph of `length` characters.

    Every character not named by a run is `user`, which is why `user` is never stored.
    Offsets are clamped to the paragraph: a run reaching past the end of a file that was
    edited by hand describes text that is no longer there, and must not raise here —
    `check` is where a file is judged, and a load must not fail on one.

    A kind this build does not know is carried through as it is, the same way an unknown
    section is (§7.2). It is a label, and dropping it would lose a newer build's record.
    """
    kinds = [USER] * length
    for start, end, kind in runs:
        first = max(0, min(int(start), length))
        last = max(first, min(int(end), length))
        for index in range(first, last):
            kinds[index] = kind
    return kinds


def collapse_runs(kinds: Sequence[str]) -> list[Run]:
    """The inverse of `expand_runs`: neighbouring characters of one kind become one run.

    `user` stretches are dropped rather than written, so the two directions compose:
    `collapse_runs(expand_runs(runs, n))` is `runs` normalised — merged where it was
    split, ordered, and clamped.
    """
    runs: list[Run] = []
    start = 0
    for index in range(1, len(kinds) + 1):
        if index < len(kinds) and kinds[index] == kinds[start]:
            continue
        if kinds[start] != USER:
            runs.append((start, index, kinds[start]))
        start = index
    return runs


# --- reconciling one paragraph ---------------------------------------------


def _matcher(old: str, new: str) -> difflib.SequenceMatcher:
    """A character diff of two paragraphs.

    `autojunk=False` is not optional. Left on, any character filling more than 1% of a
    sequence of 200 or more is treated as junk and refused as an anchor — in prose that
    is the space and half the alphabet, and the diff it then produces bears no useful
    relation to the edit.
    """
    return difflib.SequenceMatcher(a=old, b=new, autojunk=False)


def reconcile(old_text: str, old_runs: Sequence[Sequence], new_text: str) -> list[Run]:
    """`old_runs`, recomputed against `new_text`.

    A character that survived the edit keeps the kind it had; an inserted one is `user`,
    because the writer typed it; a deleted one contributes nothing. So an insertion
    before a run shifts it, an insertion inside one splits it in two, and replacing a
    whole run leaves nothing of it.

    This is the one correct answer to a stale hash. Re-hashing the new text instead would
    record "these runs belong to this prose" while the offsets still describe the old
    prose — turning a detectable problem into a silent wrong answer.
    """
    old_kinds = expand_runs(old_runs, len(old_text))
    if old_text == new_text:
        return collapse_runs(old_kinds)

    new_kinds = [USER] * len(new_text)
    for tag, old_start, old_end, new_start, new_end in _matcher(old_text, new_text).get_opcodes():
        if tag == "equal":
            new_kinds[new_start:new_end] = old_kinds[old_start:old_end]
    return collapse_runs(new_kinds)


def similarity(old_text: str, new_text: str) -> float:
    """How alike two paragraphs are, 0.0 to 1.0, for deciding which became which."""
    return _matcher(old_text, new_text).ratio()


def without_recovery(body: str, metadata: Metadata) -> Metadata:
    """The metadata `body` would have with no history to consult.

    Every paragraph whose hash is stored keeps its runs; every paragraph whose hash is
    not gets an empty list and renders as plain prose. Keys for paragraphs that are no
    longer in the body are dropped. This is what a notes file always gets — its root is
    not a repository, so there is nothing to recover from — and what a story file gets
    where the walk finds no match.
    """
    return {
        digest: list(metadata.get(digest, []))
        for digest in hashes_of(body)
    }


# --- the section on disk ---------------------------------------------------


def render_section(metadata: Metadata) -> str:
    """`metadata` as the text of an `ink:provenance` section.

    One line per paragraph, in paragraph order, the runs in JSON — which is YAML, and
    is the flow style §7.3's example is written in, so a line stays one line however
    many runs a paragraph has.

    **The key is quoted.** A hash is sixteen hex characters, and roughly one in 1845 of
    them is all digits, which YAML reads back as an integer rather than a string. Left
    bare, that paragraph's provenance would be lost on the next load for no reason the
    file shows.
    """
    return "".join(
        f"{json.dumps(digest)}: {json.dumps([[int(start), int(end), kind] for start, end, kind in runs])}\n"
        for digest, runs in metadata.items()
    )


def _offset(value: object) -> int | None:
    """One end of a run, as an integer, or `None` where it is not a whole number."""
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _runs_in(digest: str, value: object) -> list[Run]:
    """One entry's runs, dropping anything that is not `[start, end, kind]`."""
    if not isinstance(value, list):
        logger.warning("provenance for %s is not a list of runs: %r", digest, value)
        return []
    runs: list[Run] = []
    for item in value:
        start = _offset(item[0]) if isinstance(item, list) and len(item) == 3 else None
        end = _offset(item[1]) if start is not None else None
        if start is None or end is None or not isinstance(item[2], str):
            logger.warning(
                "provenance for %s has a run that is not [start, end, kind]: %r", digest, item
            )
            continue
        runs.append((start, end, item[2]))
    return runs


def parse_section(text: str) -> Metadata | None:
    """An `ink:provenance` section's text as paragraph hashes to runs.

    `None` where the section cannot be read at all — it is not YAML, or it is not a
    mapping. That is the same answer as a file with no section: the prose is untouched
    and every paragraph renders plain, which is recoverable (§7.5), where guessing at a
    broken section would not be. An empty section is `{}`, which is a different thing: a
    file the editor has saved whose prose is all hand-written.

    **Nothing is resolved to a type YAML guessed at.** `BaseLoader` reads every scalar
    as the text it is written as, and the offsets are converted here instead, because
    every way YAML resolves a scalar by itself is wrong for a hash:

    - `1234567890123456` becomes an integer, which then matches no paragraph. Roughly
      one hash in 1845 is all digits.
    - `0012345670123456` becomes *octal*, so it reads back as a different number
      entirely — wrong rather than merely unusable, and a file holding both the quoted
      and the bare spelling of one hash would split it into two entries.
    - `yes` and `no` become booleans. Not reachable from a hash, but the same mistake.

    `render_section` quotes the key so none of this arises in a file this writes. This
    is what makes a file edited by hand, or written by an older build, read correctly
    anyway.
    """
    try:
        loaded = yaml.load(text, Loader=yaml.BaseLoader)
    except yaml.YAMLError as error:
        logger.warning("the provenance section could not be parsed: %s", error)
        return None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        logger.warning("the provenance section is not a mapping: %r", loaded)
        return None
    return {str(key): _runs_in(str(key), value) for key, value in loaded.items()}
