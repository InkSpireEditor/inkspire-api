# -*- coding: utf-8 -*-
"""The `.ink` file: how its sections are read and written, and what `check` says.

These tests are text in, text out. Nothing here touches a repository; the scan that
reads these headers is in `test_storage.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from inkspire_api import ink

META = ink.fence_line(ink.SECTION_META)
BODY = ink.fence_line(ink.SECTION_BODY)
PROV = ink.fence_line(ink.SECTION_PROVENANCE)


# --- what is a fence line --------------------------------------------------


def test_a_fence_line_names_its_section() -> None:
    assert ink.section_name("===== ink:body\n") == "body"


@pytest.mark.parametrize(
    "line",
    [
        "==== ink:body\n",
        "====== ink:body\n",
        "======ink:body\n",
        "=====ink:body\n",
        "=====\tink:body\n",
        " ===== ink:body\n",
        "===== body\n",
        "===== ink:\n",
        "===== ink:Body\n",
        "===== ink:bo-dy\n",
        "===== ink:body extra\n",
        "===== ink:body\t\n",
        "She had not opened it.\n",
        "---\n",
    ],
)
def test_a_line_that_is_not_a_fence_is_prose(line: str) -> None:
    """Exactly five `=`, one space, `ink:`, a lowercase name, then nothing but spaces."""
    assert ink.section_name(line) is None


def test_trailing_spaces_on_a_fence_are_allowed() -> None:
    assert ink.section_name("=====   ink:body   \n") == "body"


def test_a_name_may_be_thirty_two_characters_and_no_more() -> None:
    assert ink.section_name(f"===== ink:{'a' * 32}\n") == "a" * 32
    assert ink.section_name(f"===== ink:{'a' * 33}\n") is None


def test_an_underscore_is_part_of_a_name() -> None:
    assert ink.section_name("===== ink:my_own_section\n") == "my_own_section"


def test_a_fence_line_needs_no_newline_at_the_end_of_a_file() -> None:
    assert ink.section_name("===== ink:body") == "body"


# --- a file with no sections -----------------------------------------------


def test_prose_alone_is_all_body() -> None:
    document = ink.parse("She had not opened it.\n")
    assert document == ink.Document(metadata={}, body="She had not opened it.\n", sections={})


def test_a_markdown_rule_in_prose_is_prose() -> None:
    """`---` is free now: it is a horizontal rule and nothing else."""
    text = "She had not opened it.\n\n---\n\nNor had he.\n"
    assert ink.parse(text) == ink.Document(metadata={}, body=text, sections={})


def test_an_empty_file_is_an_empty_body() -> None:
    assert ink.parse("") == ink.Document(metadata={}, body="", sections={})


def test_prose_above_the_first_fence_leaves_the_whole_file_as_prose() -> None:
    """Nothing is lost: the text a writer typed is still all there, unparsed."""
    text = f"She had not opened it.\n{BODY}Nor had he.\n"
    assert ink.parse(text) == ink.Document(metadata={}, body=text, sections={})


# --- a header --------------------------------------------------------------


def test_a_title_is_read_and_the_prose_kept() -> None:
    document = ink.parse(f"{META}title: The Letter\n{BODY}She had not opened it.\n")
    assert document.metadata == {"title": "The Letter"}
    assert document.body == "She had not opened it.\n"


def test_all_three_keys_are_read() -> None:
    document = ink.parse(
        f"{META}title: The Letter\nstatus: draft\nsummary: She opens it.\n{BODY}Once.\n"
    )
    assert document.metadata == {
        "title": "The Letter",
        "status": "draft",
        "summary": "She opens it.",
    }


def test_an_empty_meta_section_is_a_header_with_nothing_in_it() -> None:
    assert ink.parse(f"{META}{BODY}Once.\n") == ink.Document(body="Once.\n")


def test_no_section_is_required() -> None:
    assert ink.parse(f"{BODY}Once.\n").body == "Once.\n"
    assert ink.parse(f"{META}title: One\n").metadata == {"title": "One"}
    assert ink.parse(f"{META}title: One\n").body == ""


def test_a_body_section_may_hold_what_used_to_look_like_a_header() -> None:
    document = ink.parse(f"{BODY}---\ntitle: not a header\n---\nOnce.\n")
    assert document.metadata == {}
    assert document.body == "---\ntitle: not a header\n---\nOnce.\n"


def test_windows_line_endings_are_read_and_kept() -> None:
    document = ink.parse(f"{META}title: The Letter\r\n{BODY}Once.\r\nTwice.\r\n")
    assert document.metadata == {"title": "The Letter"}
    assert document.body == "Once.\r\nTwice.\r\n"


def test_a_header_key_may_hold_several_lines() -> None:
    document = ink.parse(
        f"{META}summary: |\n  She opens it.\n  It is not what she was told.\n{BODY}Once.\n"
    )
    assert document.metadata == {"summary": "She opens it.\nIt is not what she was told.\n"}


def test_accents_and_japanese_survive_a_header() -> None:
    document = ink.parse(f"{META}title: Pontochō, été\n{BODY}Once.\n")
    assert document.metadata == {"title": "Pontochō, été"}


# --- a header that cannot be read ------------------------------------------


def test_a_header_that_is_not_yaml_keeps_the_body() -> None:
    """The fences say where the prose is, so a broken header no longer swallows it."""
    document = ink.parse(f"{META}title: [unclosed\n{BODY}Once.\n")
    assert document == ink.Document(metadata={}, body="Once.\n", sections={})


def test_a_header_that_is_a_list_keeps_the_body() -> None:
    document = ink.parse(f"{META}- one\n- two\n{BODY}Once.\n")
    assert document == ink.Document(metadata={}, body="Once.\n", sections={})


# --- other sections --------------------------------------------------------


def test_a_provenance_section_is_carried_as_text() -> None:
    document = ink.parse(f'{BODY}Once.\n{PROV}47f57caaa4fb330e: [[0, 5, "gen"]]\n')
    assert document.body == "Once.\n"
    assert document.sections == {"provenance": '47f57caaa4fb330e: [[0, 5, "gen"]]\n'}


def test_a_section_this_build_does_not_know_survives_a_round_trip() -> None:
    """An older build must never delete a newer build's metadata."""
    text = f"{BODY}Once.\n===== ink:outline\nsomething new\n"
    document = ink.parse(text)
    assert document.sections == {"outline": "something new\n"}
    assert ink.render(document.metadata, document.body, document.sections) == text


def test_sections_keep_the_order_they_were_read_in() -> None:
    text = f"{BODY}Once.\n{PROV}h: []\n===== ink:outline\nx\n"
    document = ink.parse(text)
    assert list(document.sections) == ["provenance", "outline"]
    assert ink.render(document.metadata, document.body, document.sections) == text


def test_a_section_opened_twice_keeps_its_last_occurrence() -> None:
    document = ink.parse(f"{BODY}First.\n{BODY}Second.\n")
    assert document.body == "Second.\n"


# --- writing one back ------------------------------------------------------


def test_a_file_is_written_back_exactly_as_it_was_read() -> None:
    text = (
        f"{META}title: The Letter\nsummary: |\n  Two\n  lines.\npov: Jane Doe\n"
        f"{BODY}Prose — ünïcode, 日本語.\n"
        f'{PROV}47f57caaa4fb330e: [[0, 5, "gen"]]\n'
    )
    document = ink.parse(text)
    assert ink.render(document.metadata, document.body, document.sections) == text


def test_a_body_with_no_closing_newline_is_written_back_as_it_was() -> None:
    text = f"{META}title: One\n{BODY}Once."
    document = ink.parse(text)
    assert document.body == "Once."
    assert ink.render(document.metadata, document.body, document.sections) == text


def test_a_body_with_no_closing_newline_is_given_one_when_a_section_follows() -> None:
    """A fence has to start a line. This is the only byte a round trip can change."""
    rendered = ink.render({}, "Once.", {"provenance": "h: []\n"})
    assert rendered == f"{BODY}Once.\n{PROV}h: []\n"
    assert ink.parse(rendered).body == "Once.\n"
    assert ink.render({}, "Once.\n", {"provenance": "h: []\n"}) == rendered


def test_nothing_to_record_either_side_writes_no_fences() -> None:
    assert ink.render({}, "Once.\n", {}) == "Once.\n"


def test_a_section_with_no_header_still_fences_the_body() -> None:
    assert ink.render({}, "Once.\n", {"provenance": "h: []\n"}) == f"{BODY}Once.\n{PROV}h: []\n"


def test_a_key_this_module_does_not_know_is_kept() -> None:
    text = f"{META}title: One\npov: Jane Doe\n{BODY}Once.\n"
    document = ink.parse(text)
    assert ink.render(document.metadata, document.body, document.sections) == text


def test_keys_are_written_in_the_order_they_were_given() -> None:
    rendered = ink.render({"title": "One", "status": "draft"}, "", {})
    assert rendered == f"{META}title: One\nstatus: draft\n{BODY}"


def test_prose_holding_a_fence_line_refuses_to_be_written() -> None:
    """Rather than escaping it and making the prose stop being the prose."""
    with pytest.raises(ValueError, match="reads as a section fence"):
        ink.render({"title": "One"}, f"Once.\n{BODY}Twice.\n", {})


def test_prose_holding_something_that_only_looks_like_a_fence_is_fine() -> None:
    assert ink.render({}, "====== ink:body\n", {}) == "====== ink:body\n"


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
    path.write_text(f"{META}title: The Letter\n{BODY}" + "x" * 100_000, encoding="utf-8")
    assert ink.read_header(path) == {"title": "The Letter"}


def test_a_file_with_no_header_reads_as_no_metadata(tmp_path: Path) -> None:
    path = tmp_path / "first.ink"
    path.write_text("Once.\n", encoding="utf-8")
    assert ink.read_header(path) == {}


def test_a_header_longer_than_the_budget_is_not_read(tmp_path: Path) -> None:
    """The read stopped at the budget with no section after `meta`, so it was cut off."""
    path = tmp_path / "first.ink"
    path.write_text(f"{META}" + "x: y\n" * 50_000 + f"{BODY}Once.\n", encoding="utf-8")
    assert ink.read_header(path) == {}


def test_a_file_that_is_only_a_header_is_read(tmp_path: Path) -> None:
    """Short enough that the read reached the end of the file, so nothing was cut off."""
    path = tmp_path / "first.ink"
    path.write_text(f"{META}title: The Letter\n", encoding="utf-8")
    assert ink.read_header(path) == {"title": "The Letter"}


def test_a_header_below_the_first_section_is_not_read(tmp_path: Path) -> None:
    """Which is the whole reason `render` writes it first."""
    path = tmp_path / "first.ink"
    path.write_text(f"{BODY}Once.\n{META}title: The Letter\n", encoding="utf-8")
    assert ink.read_header(path) == {}


def test_a_file_that_is_not_utf8_reads_as_no_metadata(tmp_path: Path) -> None:
    path = tmp_path / "first.ink"
    path.write_bytes(META.encode("utf-8") + b"title: \xff\xfe\n")
    assert ink.read_header(path) == {}


def test_a_file_that_is_not_there_reads_as_no_metadata(tmp_path: Path) -> None:
    assert ink.read_header(tmp_path / "gone.ink") == {}


# --- check -----------------------------------------------------------------


def test_prose_alone_has_nothing_to_report() -> None:
    assert ink.check("She had not opened it.\n") == []
    assert ink.check("") == []
    assert ink.check("One.\n\n---\n\nTwo.\n") == []


def test_a_good_file_has_nothing_to_report() -> None:
    text = f"{META}title: One\nstatus: draft\nsummary: x\n{BODY}Once.\n{PROV}h: []\n"
    assert ink.check(text) == []


def test_an_empty_meta_section_has_nothing_to_report() -> None:
    assert ink.check(f"{META}{BODY}Once.\n") == []


def test_prose_above_the_first_section_is_an_error_on_the_first_line() -> None:
    problems = ink.check(f"Once.\n{BODY}Twice.\n")
    assert [(p.line, p.level) for p in problems] == [(1, ink.ERROR)]
    assert "above its first section" in problems[0].message


def test_a_header_that_is_not_yaml_is_an_error_where_yaml_says() -> None:
    problems = ink.check(f"{META}title: [unclosed\n{BODY}Once.\n")
    assert [p.level for p in problems] == [ink.ERROR]
    assert "not valid YAML" in problems[0].message


def test_a_header_that_is_not_a_mapping_is_an_error() -> None:
    problems = ink.check(f"{META}- one\n{BODY}Once.\n")
    assert [(p.line, p.level) for p in problems] == [(2, ink.ERROR)]
    assert "mapping" in problems[0].message


def test_a_key_that_is_not_text_is_an_error_on_its_own_line() -> None:
    problems = ink.check(f"{META}title: One\nstatus: 3\n{BODY}Once.\n")
    assert [(p.line, p.level) for p in problems] == [(3, ink.ERROR)]
    assert '"status" has to be text' in problems[0].message


def test_a_title_over_two_lines_is_an_error() -> None:
    problems = ink.check(f"{META}title: |\n  One\n  Two\n{BODY}Once.\n")
    assert [(p.line, p.level) for p in problems] == [(2, ink.ERROR)]
    assert "two lines" in problems[0].message


def test_an_unknown_key_is_a_warning() -> None:
    problems = ink.check(f"{META}title: One\npov: Jane Doe\n{BODY}Once.\n")
    assert [(p.line, p.level) for p in problems] == [(3, ink.WARNING)]
    assert '"pov"' in problems[0].message


def test_an_unknown_section_is_a_warning_on_its_fence() -> None:
    problems = ink.check(f"{BODY}Once.\n===== ink:outline\nx\n")
    assert [(p.line, p.level) for p in problems] == [(3, ink.WARNING)]
    assert '"outline"' in problems[0].message


def test_a_section_opened_twice_is_an_error_on_the_second_fence() -> None:
    problems = ink.check(f"{BODY}First.\n{BODY}Second.\n")
    assert [(p.line, p.level) for p in problems] == [(3, ink.ERROR)]
    assert "more than once" in problems[0].message


def test_a_header_below_the_first_section_is_an_error() -> None:
    problems = ink.check(f"{BODY}Once.\n{META}title: One\n")
    assert [(p.line, p.level) for p in problems] == [(3, ink.ERROR)]
    assert "come first" in problems[0].message


def test_every_problem_is_reported_in_line_order() -> None:
    problems = ink.check(f"{META}pov: Jane Doe\nstatus: 3\n{BODY}Once.\n")
    assert [(p.line, p.level) for p in problems] == [(2, ink.WARNING), (3, ink.ERROR)]


# --- writing a header ------------------------------------------------------


def test_a_field_is_written_into_an_empty_header() -> None:
    text = ink.with_header(ink.parse("Once.\n"), "one", {"status": "draft"})
    assert text == f"{META}status: draft\n{BODY}Once.\n"


def test_a_field_already_saying_that_writes_nothing() -> None:
    """`None` rather than the same text, so a caller can skip the write."""
    document = ink.parse(f"{META}status: draft\n{BODY}Once.\n")
    assert ink.with_header(document, "one", {"status": "draft"}) is None


def test_an_empty_value_removes_the_field() -> None:
    document = ink.parse(f"{META}status: draft\n{BODY}Once.\n")
    assert ink.with_header(document, "one", {"status": ""}) == "Once.\n"


def test_a_title_the_filename_already_says_is_dropped() -> None:
    """Renaming a file to what it is already called leaves it with no header."""
    document = ink.parse(f"{META}title: Something Else\n{BODY}Once.\n")
    assert ink.with_header(document, "one", {"title": "one"}) == "Once.\n"


def test_removing_the_last_header_field_keeps_the_other_sections() -> None:
    """Dropping the fences would take the provenance section with them."""
    document = ink.parse(f"{META}status: draft\n{BODY}Once.\n{PROV}h: []\n")
    assert ink.with_header(document, "one", {"status": ""}) == f"{BODY}Once.\n{PROV}h: []\n"


def test_a_title_a_filename_cannot_say_is_written() -> None:
    text = ink.with_header(ink.parse("Once.\n"), "the-letter", {"title": "The Letter"})
    assert text == f"{META}title: The Letter\n{BODY}Once.\n"


def test_a_field_not_mentioned_is_left_alone() -> None:
    """Which is what keeps a status set by hand while a rename is being saved."""
    document = ink.parse(f"{META}status: revised\n{BODY}Once.\n")
    text = ink.with_header(document, "one", {"title": "The Letter"})
    assert text == f"{META}status: revised\ntitle: The Letter\n{BODY}Once.\n"


def test_a_key_this_module_does_not_know_survives_a_header_write() -> None:
    document = ink.parse(f"{META}pov: Jane Doe\n{BODY}Once.\n")
    text = ink.with_header(document, "one", {"status": "draft"})
    assert text == f"{META}pov: Jane Doe\nstatus: draft\n{BODY}Once.\n"


def test_a_provenance_section_survives_a_header_write() -> None:
    document = ink.parse(f"{BODY}Once.\n{PROV}h: []\n")
    text = ink.with_header(document, "one", {"status": "draft"})
    assert text == f"{META}status: draft\n{BODY}Once.\n{PROV}h: []\n"


def test_several_fields_are_applied_in_one_pass() -> None:
    text = ink.with_header(
        ink.parse("Once.\n"),
        "one",
        {"title": "The Letter", "status": "draft", "summary": "She opens it."},
    )
    assert text == f"{META}title: The Letter\nstatus: draft\nsummary: She opens it.\n{BODY}Once.\n"
