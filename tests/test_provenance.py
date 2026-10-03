# -*- coding: utf-8 -*-
"""Where one paragraph ends and what it hashes to.

The vectors in `tests/data/paragraphs.json` are the shared answer: the frontend's own
`provenance.ts` has a byte-identical copy of that file and asserts against it, so a
change to either implementation that is not a change to the file breaks one of the two
suites. The file is the specification — its expectations were written by hand and
checked to rebuild each body, not recorded from this module's output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inkspire_api.provenance import (
    hashes_of,
    join_paragraphs,
    paragraph_hash,
    split_paragraphs,
)

VECTORS = json.loads(
    (Path(__file__).parent / "data" / "paragraphs.json").read_text(encoding="utf-8")
)
CASES = pytest.mark.parametrize("case", VECTORS, ids=[v["name"] for v in VECTORS])


# --- the shared vectors ----------------------------------------------------


@CASES
def test_a_body_splits_where_the_vectors_say(case: dict) -> None:
    paragraphs, separators = split_paragraphs(case["body"])
    assert paragraphs == case["paragraphs"]
    assert separators == case["separators"]


@CASES
def test_a_paragraph_hashes_to_what_the_vectors_say(case: dict) -> None:
    assert hashes_of(case["body"]) == case["hashes"]


@CASES
def test_a_split_body_is_rebuilt_byte_for_byte(case: dict) -> None:
    paragraphs, separators = split_paragraphs(case["body"])
    assert join_paragraphs(paragraphs, separators) == case["body"]


@CASES
def test_there_is_one_more_separator_than_there_are_paragraphs(case: dict) -> None:
    """Which is what carries a leading blank line and a closing newline."""
    paragraphs, separators = split_paragraphs(case["body"])
    assert len(separators) == len(paragraphs) + 1


# --- the closing newline ---------------------------------------------------


def test_a_closing_newline_is_not_part_of_the_last_paragraph() -> None:
    """`ink.render` adds one where a section follows the body, so a hash that counted
    it would move on the first save."""
    assert split_paragraphs("One.")[0] == ["One."]
    assert split_paragraphs("One.\n")[0] == ["One."]
    assert paragraph_hash("One.") == hashes_of("One.\n")[0]


def test_only_one_closing_newline_is_held_out() -> None:
    """A second one is a paragraph separator like any other run, and there is nothing
    after it to separate."""
    paragraphs, separators = split_paragraphs("One.\n\n")
    assert paragraphs == ["One."]
    assert separators == ["", "\n\n"]


# --- what separates a paragraph --------------------------------------------


def test_a_single_line_break_keeps_a_paragraph_whole() -> None:
    assert split_paragraphs("One.\nStill one.")[0] == ["One.\nStill one."]


def test_two_line_endings_separate_whatever_their_flavour() -> None:
    for separator in ["\n\n", "\r\n\r\n", "\r\n\n", "\n\r\n"]:
        assert split_paragraphs(f"One.{separator}Two.")[0] == ["One.", "Two."]


def test_a_lone_carriage_return_is_not_a_line_ending() -> None:
    """`.ink` carries `LF` and `CRLF`. A bare `\\r` is text."""
    assert split_paragraphs("One.\r\rTwo.")[0] == ["One.\r\rTwo."]


def test_blank_lines_made_only_of_spaces_do_not_separate() -> None:
    """A line holding a space is a line holding a space, not an empty one."""
    assert split_paragraphs("One.\n \nTwo.")[0] == ["One.\n \nTwo."]


# --- the hash --------------------------------------------------------------


def test_a_hash_is_sixteen_hex_characters() -> None:
    digest = paragraph_hash("One.")
    assert len(digest) == 16
    assert set(digest) <= set("0123456789abcdef")


def test_the_same_text_hashes_the_same_way_twice() -> None:
    assert paragraph_hash("One.") == paragraph_hash("One.")


def test_two_identical_paragraphs_share_one_key() -> None:
    """Accepted: they cannot be told apart, so they cannot hold different runs."""
    assert hashes_of("Same.\n\nSame.\n") == [paragraph_hash("Same.")] * 2


def test_a_paragraph_that_differs_by_one_character_hashes_differently() -> None:
    assert paragraph_hash("One.") != paragraph_hash("One!")


def test_text_outside_ascii_is_not_flattened_before_hashing() -> None:
    """An encode that dropped or replaced what it could not carry would give two
    different paragraphs one key. The vectors pin the encoding itself: their hashes were
    computed from UTF-8 bytes by hand, not taken from this module."""
    assert paragraph_hash("été") != paragraph_hash("ete")
    assert paragraph_hash("日本語") != paragraph_hash("???")


# --- joining back ----------------------------------------------------------


def test_joining_the_wrong_number_of_separators_is_refused() -> None:
    """Rather than losing or inventing text where a caller got the shape wrong."""
    with pytest.raises(ValueError, match="need 3 separators"):
        join_paragraphs(["One.", "Two."], ["", "\n\n"])


def test_joining_nothing_gives_the_separator_it_was_handed() -> None:
    assert join_paragraphs([], ["\n\n\n"]) == "\n\n\n"
