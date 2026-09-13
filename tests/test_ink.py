# -*- coding: utf-8 -*-
"""The `.ink` file: what its header means, and what `check` says about a broken one.

These tests are text in, text out. Nothing here touches a repository; the scan that
reads these headers is in `test_storage.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inkspire_api import ink

# --- no header -------------------------------------------------------------


def test_prose_alone_is_all_body() -> None:
    document = ink.parse("She had not opened it.\n")
    assert document.metadata == {}
    assert document.body == "She had not opened it.\n"


def test_a_fence_below_the_first_line_is_prose() -> None:
    """Front matter is front matter: a rule below the first line is a horizontal rule."""
    text = "She had not opened it.\n---\nNor had he.\n"
    assert ink.parse(text) == ink.Document(metadata={}, body=text)


def test_an_empty_file_is_an_empty_body() -> None:
    assert ink.parse("") == ink.Document(metadata={}, body="")


# --- a header --------------------------------------------------------------


def test_a_title_is_read_and_the_prose_kept() -> None:
    document = ink.parse("---\ntitle: The Letter\n---\nShe had not opened it.\n")
    assert document.metadata == {"title": "The Letter"}
    assert document.body == "She had not opened it.\n"


def test_all_three_keys_are_read() -> None:
    document = ink.parse(
        "---\ntitle: The Letter\nstatus: draft\nsummary: She opens it.\n---\nOnce.\n"
    )
    assert document.metadata == {
        "title": "The Letter",
        "status": "draft",
        "summary": "She opens it.",
    }


def test_an_empty_header_is_a_header_with_nothing_in_it() -> None:
    assert ink.parse("---\n---\nOnce.\n") == ink.Document(metadata={}, body="Once.\n")


def test_a_header_ends_at_the_first_closing_fence() -> None:
    document = ink.parse("---\ntitle: One\n---\nOnce.\n---\nTwice.\n")
    assert document.metadata == {"title": "One"}
    assert document.body == "Once.\n---\nTwice.\n"


def test_windows_line_endings_are_read_and_kept() -> None:
    document = ink.parse("---\r\ntitle: The Letter\r\n---\r\nOnce.\r\n")
    assert document.metadata == {"title": "The Letter"}
    assert document.body == "Once.\r\n"


def test_a_file_of_nothing_but_a_header_has_no_prose() -> None:
    assert ink.parse("---\ntitle: One\n---\n").body == ""


def test_a_header_key_may_hold_several_lines() -> None:
    document = ink.parse(
        "---\nsummary: |\n  She opens it.\n  It is not what she was told.\n---\nOnce.\n"
    )
    assert document.metadata == {
        "summary": "She opens it.\nIt is not what she was told.\n"
    }


def test_accents_and_japanese_survive_a_header() -> None:
    document = ink.parse("---\ntitle: Pontochō, été\n---\nOnce.\n")
    assert document.metadata == {"title": "Pontochō, été"}


# --- a header that cannot be read ------------------------------------------


def test_an_unclosed_header_leaves_the_whole_file_as_prose() -> None:
    """Nothing is lost: the text a writer typed is still all there, unparsed."""
    text = "---\ntitle: The Letter\nShe had not opened it.\n"
    assert ink.parse(text) == ink.Document(metadata={}, body=text)


def test_a_header_that_is_not_yaml_leaves_the_whole_file_as_prose() -> None:
    text = "---\ntitle: [unclosed\n---\nOnce.\n"
    assert ink.parse(text) == ink.Document(metadata={}, body=text)


def test_a_header_that_is_a_list_leaves_the_whole_file_as_prose() -> None:
    text = "---\n- one\n- two\n---\nOnce.\n"
    assert ink.parse(text) == ink.Document(metadata={}, body=text)


# --- writing one back ------------------------------------------------------


def test_a_file_is_written_back_exactly_as_it_was_read() -> None:
    text = (
        "---\ntitle: The Letter\nsummary: |\n  Two\n  lines.\npov: Jane Doe\n"
        "---\nProse — ünïcode, 日本語.\n"
    )
    document = ink.parse(text)
    assert ink.render(document.metadata, document.body) == text


def test_nothing_to_record_writes_no_fences() -> None:
    assert ink.render({}, "Once.\n") == "Once.\n"


def test_a_key_this_module_does_not_know_is_kept() -> None:
    document = ink.parse("---\ntitle: One\npov: Jane Doe\n---\nOnce.\n")
    assert ink.render(document.metadata, document.body) == (
        "---\ntitle: One\npov: Jane Doe\n---\nOnce.\n"
    )


def test_keys_are_written_in_the_order_they_were_given() -> None:
    rendered = ink.render({"title": "One", "status": "draft"}, "")
    assert rendered == "---\ntitle: One\nstatus: draft\n---\n"


# --- the name a file is shown under ----------------------------------------


def test_a_title_is_the_name() -> None:
    assert ink.display_name({"title": "The Letter"}, "first-chapter") == "The Letter"


def test_a_file_with_no_title_is_named_by_its_filename() -> None:
    assert ink.display_name({}, "first-chapter") == "first-chapter"


@pytest.mark.parametrize("title", ["", "   ", None, 5, ["The Letter"]])
def test_a_title_that_is_not_a_name_falls_back_to_the_filename(title: object) -> None:
    assert ink.display_name({"title": title}, "first-chapter") == "first-chapter"


def test_a_title_is_shown_without_the_space_around_it() -> None:
    assert ink.display_name({"title": "  The Letter  "}, "stem") == "The Letter"


# --- reading only the header -----------------------------------------------


def test_a_header_is_read_without_the_prose_under_it(tmp_path: Path) -> None:
    path = tmp_path / "first.ink"
    path.write_text("---\ntitle: The Letter\n---\n" + "x" * 100_000, encoding="utf-8")
    assert ink.read_header(path) == {"title": "The Letter"}


def test_a_file_with_no_header_reads_as_no_metadata(tmp_path: Path) -> None:
    path = tmp_path / "first.ink"
    path.write_text("Once.\n", encoding="utf-8")
    assert ink.read_header(path) == {}


def test_a_header_longer_than_the_budget_is_not_read(tmp_path: Path) -> None:
    """A file opening with a fence and megabytes of text is not a header."""
    path = tmp_path / "first.ink"
    path.write_text("---\n" + "x: y\n" * 50_000 + "---\nOnce.\n", encoding="utf-8")
    assert ink.read_header(path) == {}


def test_a_file_that_is_not_utf8_reads_as_no_metadata(tmp_path: Path) -> None:
    path = tmp_path / "first.ink"
    path.write_bytes(b"---\ntitle: \xff\xfe\n---\n")
    assert ink.read_header(path) == {}


def test_a_file_that_is_not_there_reads_as_no_metadata(tmp_path: Path) -> None:
    assert ink.read_header(tmp_path / "gone.ink") == {}


# --- check -----------------------------------------------------------------


def test_prose_alone_has_nothing_to_report() -> None:
    assert ink.check("She had not opened it.\n") == []


def test_a_good_header_has_nothing_to_report() -> None:
    assert ink.check("---\ntitle: One\nstatus: draft\nsummary: x\n---\nOnce.\n") == []


def test_an_empty_header_has_nothing_to_report() -> None:
    assert ink.check("---\n---\nOnce.\n") == []


def test_an_unclosed_header_is_an_error_on_the_first_line() -> None:
    problems = ink.check("---\ntitle: One\nOnce.\n")
    assert [(p.line, p.level) for p in problems] == [(1, ink.ERROR)]
    assert "never closed" in problems[0].message


def test_a_header_that_is_not_yaml_is_an_error_where_yaml_says() -> None:
    problems = ink.check("---\ntitle: [unclosed\n---\nOnce.\n")
    assert [p.level for p in problems] == [ink.ERROR]
    assert "not valid YAML" in problems[0].message


def test_a_header_that_is_not_a_mapping_is_an_error() -> None:
    problems = ink.check("---\n- one\n---\nOnce.\n")
    assert [(p.line, p.level) for p in problems] == [(2, ink.ERROR)]
    assert "mapping" in problems[0].message


def test_a_key_that_is_not_text_is_an_error_on_its_own_line() -> None:
    problems = ink.check("---\ntitle: One\nstatus: 3\n---\nOnce.\n")
    assert [(p.line, p.level) for p in problems] == [(3, ink.ERROR)]
    assert '"status" has to be text' in problems[0].message


def test_a_title_over_two_lines_is_an_error() -> None:
    problems = ink.check("---\ntitle: |\n  One\n  Two\n---\nOnce.\n")
    assert [(p.line, p.level) for p in problems] == [(2, ink.ERROR)]
    assert "two lines" in problems[0].message


def test_an_unknown_key_is_a_warning() -> None:
    problems = ink.check("---\ntitle: One\npov: Jane Doe\n---\nOnce.\n")
    assert [(p.line, p.level) for p in problems] == [(3, ink.WARNING)]
    assert '"pov"' in problems[0].message


def test_prose_opening_on_a_fence_is_a_warning() -> None:
    problems = ink.check("---\ntitle: One\n---\n---\nOnce.\n")
    assert [(p.line, p.level) for p in problems] == [(4, ink.WARNING)]
    assert "second header" in problems[0].message


def test_every_problem_is_reported_in_line_order() -> None:
    problems = ink.check("---\npov: Jane Doe\nstatus: 3\n---\nOnce.\n")
    assert [(p.line, p.level) for p in problems] == [(2, ink.WARNING), (3, ink.ERROR)]
