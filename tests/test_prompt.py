# -*- coding: utf-8 -*-
"""Assembly and the trim, the policy in `inkspire_api/prompt.py`.

The render itself is pinned character for character in `tests/test_llm.py`, against
`tests/data/prompts.json`; these tests are about what reaches the render, not what it
produces.
"""

from __future__ import annotations

import pytest

from inkspire_api.prompt import (
    Cursor,
    CursorOutOfRange,
    CursorRange,
    InvertedRange,
    PromptContext,
    assemble,
    count_words,
    cursor_from_offset,
    render,
    split_at_cursor,
    split_at_range,
    trim_to_head,
    trim_to_tail,
)
from inkspire_api import provenance


def test_under_budget_the_body_is_returned_unchanged() -> None:
    body = "First paragraph.\n\nSecond paragraph.\n"
    assert trim_to_tail(body, budget=len(body)) == body
    assert trim_to_tail(body, budget=len(body) + 100) == body


def test_an_empty_body_stays_empty() -> None:
    assert trim_to_tail("", budget=10) == ""


def test_a_blank_only_body_under_budget_is_unchanged() -> None:
    assert trim_to_tail("\n\n\n", budget=10) == "\n\n\n"


def test_over_budget_only_whole_paragraphs_survive_from_the_tail() -> None:
    body = "One.\n\nTwo.\n\nThree.\n\nFour.\n"
    # "Four.\n" (6) + "Three.\n\n" before it (8) = 14, fits; "Two.\n\n" (6) more = 20,
    # still fits; "One.\n\n" (6) more = 26, which would not -- so One. must be dropped.
    trimmed = trim_to_tail(body, budget=20)
    assert trimmed == "Two.\n\nThree.\n\nFour.\n"


def test_the_trim_never_opens_on_a_blank_line() -> None:
    body = "One.\n\nTwo.\n\nThree.\n"
    trimmed = trim_to_tail(body, budget=len("Three.\n") + 1)
    assert trimmed == "Three.\n"
    assert not trimmed.startswith("\n")


def test_the_trim_keeps_the_bodys_own_ending_exactly() -> None:
    """A trailing newline, or its absence, is part of the body and must survive."""
    with_newline = "One.\n\nTwo.\n"
    without_newline = "One.\n\nTwo."
    assert trim_to_tail(with_newline, budget=len("Two.\n")).endswith("Two.\n")
    assert trim_to_tail(without_newline, budget=len("Two.")).endswith("Two.")


def test_the_first_kept_paragraph_is_always_whole() -> None:
    body = "One.\n\nTwo.\n\nThree.\n"
    # A budget that would only fit part of "Two." still keeps it whole, not clipped.
    trimmed = trim_to_tail(body, budget=len("Two.\n\nThree.\n") - 1)
    assert trimmed in ("Two.\n\nThree.\n", "Three.\n")
    assert not trimmed.startswith("o.")  # never a mid-paragraph cut


@pytest.mark.parametrize("budget", [1, 5, 20, 100])
def test_the_result_never_exceeds_the_budget_by_more_than_one_paragraph(
    budget: int,
) -> None:
    """A whole paragraph may be kept even if it alone is larger than the budget
    (the "always at least one" rule #12 also specifies) -- but never two."""
    body = "Short.\n\n" + ("Word " * 50) + "\n\nTail.\n"
    trimmed = trim_to_tail(body, budget=budget)
    assert trimmed == "Tail.\n" or len(trimmed) <= budget or trimmed.strip() == "Tail."


def test_a_single_paragraph_over_budget_falls_to_whole_lines() -> None:
    body = "Line one is here.\nLine two is here.\nLine three is here.\n"
    # Budget fits the last two lines but not all three.
    budget = len("Line two is here.\nLine three is here.\n")
    trimmed = trim_to_tail(body, budget=budget)
    assert trimmed == "Line two is here.\nLine three is here.\n"


def test_a_single_line_over_budget_falls_to_a_hard_character_cut() -> None:
    body = "x" * 500 + "\n"
    trimmed = trim_to_tail(body, budget=50)
    assert trimmed.endswith("x" * 49 + "\n")
    assert len(trimmed) == 50


def test_a_hard_cut_still_keeps_the_tail_when_the_budget_is_tiny() -> None:
    body = "abcdefghij\n"
    trimmed = trim_to_tail(body, budget=3)
    assert trimmed.endswith("j\n")


def test_assemble_wraps_the_trimmed_text_in_a_context() -> None:
    assert assemble("hello", budget=10) == PromptContext(prefix="hello", suffix="")
    assert assemble("hello world, this is long", budget=5) != PromptContext(
        prefix="hello world, this is long", suffix=""
    )


def test_render_ends_on_the_contexts_last_character() -> None:
    assert render(PromptContext(prefix="the house was")).endswith("the house was")


# --- trim_to_head, the mirror of trim_to_tail -------------------------------


def test_trim_to_head_under_budget_is_unchanged() -> None:
    body = "First paragraph.\n\nSecond paragraph.\n"
    assert trim_to_head(body, budget=len(body)) == body


def test_trim_to_head_keeps_whole_paragraphs_from_the_start() -> None:
    body = "One.\n\nTwo.\n\nThree.\n\nFour.\n"
    # "One." (4) + "\n\nTwo." (6) more = 10, fits; + "\n\nThree." (8) more = 18, still
    # fits; + "\n\nFour." (7) more = 25, which would not -- so Four. must be dropped,
    # and with it the separator that would have joined it on, per the next test.
    trimmed = trim_to_head(body, budget=20)
    assert trimmed == "One.\n\nTwo.\n\nThree."


def test_trim_to_head_never_ends_on_a_blank_line() -> None:
    """The separator that would join on the next paragraph is dropped outright, not
    partially kept, even where there would be room for part of it."""
    body = "One.\n\nTwo.\n\nThree.\n"
    trimmed = trim_to_head(body, budget=len("One.\n\n") + 1)
    assert trimmed == "One."
    assert not trimmed.endswith("\n")


def test_trim_to_head_keeps_the_bodys_own_leading_blank_lines() -> None:
    body = "\n\nOne.\n\nTwo.\n"
    trimmed = trim_to_head(body, budget=len("\n\nOne."))
    assert trimmed == "\n\nOne."


def test_a_single_paragraph_over_budget_falls_to_whole_lines_from_the_head() -> None:
    body = "Line one is here.\nLine two is here.\nLine three is here.\n"
    budget = len("Line one is here.\nLine two is here.\n")
    trimmed = trim_to_head(body, budget=budget)
    assert trimmed == "Line one is here.\nLine two is here."


def test_a_single_line_over_budget_falls_to_a_hard_character_cut_from_the_head() -> None:
    body = "x" * 500 + "\n"
    trimmed = trim_to_head(body, budget=50)
    assert trimmed.startswith("x" * 49)
    assert len(trimmed) == 50


# --- the cursor --------------------------------------------------------------


def test_split_at_cursor_at_a_paragraphs_start() -> None:
    paras, seps = provenance.split_paragraphs("One.\n\nTwo.\n\nThree.\n")
    p_paras, p_seps, s_paras, s_seps = split_at_cursor(paras, seps, Cursor(para=1, offset=0))
    assert provenance.join_paragraphs(p_paras, p_seps) == "One.\n\n"
    assert provenance.join_paragraphs(s_paras, s_seps) == "Two.\n\nThree.\n"


def test_split_at_cursor_in_the_middle_of_a_paragraph() -> None:
    paras, seps = provenance.split_paragraphs("One.\n\nTwo.\n\nThree.\n")
    p_paras, p_seps, s_paras, s_seps = split_at_cursor(paras, seps, Cursor(para=1, offset=2))
    assert provenance.join_paragraphs(p_paras, p_seps) == "One.\n\nTw"
    assert provenance.join_paragraphs(s_paras, s_seps) == "o.\n\nThree.\n"


def test_split_at_cursor_at_a_paragraphs_end() -> None:
    paras, seps = provenance.split_paragraphs("One.\n\nTwo.\n\nThree.\n")
    p_paras, p_seps, s_paras, s_seps = split_at_cursor(paras, seps, Cursor(para=0, offset=4))
    assert provenance.join_paragraphs(p_paras, p_seps) == "One."
    assert provenance.join_paragraphs(s_paras, s_seps) == "\n\nTwo.\n\nThree.\n"


def test_split_at_cursor_rejects_an_out_of_range_paragraph() -> None:
    paras, seps = provenance.split_paragraphs("One.\n\nTwo.\n")
    with pytest.raises(CursorOutOfRange):
        split_at_cursor(paras, seps, Cursor(para=5, offset=0))
    with pytest.raises(CursorOutOfRange):
        split_at_cursor(paras, seps, Cursor(para=-1, offset=0))


def test_split_at_cursor_rejects_an_out_of_range_offset() -> None:
    paras, seps = provenance.split_paragraphs("One.\n\nTwo.\n")
    with pytest.raises(CursorOutOfRange):
        split_at_cursor(paras, seps, Cursor(para=0, offset=99))
    with pytest.raises(CursorOutOfRange):
        split_at_cursor(paras, seps, Cursor(para=0, offset=-1))


@pytest.mark.parametrize(
    ("offset", "expected"),
    [
        (0, Cursor(para=0, offset=0)),
        (3, Cursor(para=0, offset=3)),  # inside "One."
        (4, Cursor(para=0, offset=4)),  # exactly at its end
        (5, Cursor(para=0, offset=4)),  # inside the separator -- the end of "One."
        (6, Cursor(para=1, offset=0)),  # exactly at the start of "Two."
        (100, Cursor(para=1, offset=4)),  # past the end -- the end of the last paragraph
    ],
)
def test_cursor_from_offset(offset: int, expected: Cursor) -> None:
    assert cursor_from_offset("One.\n\nTwo.\n", offset) == expected


def test_cursor_from_offset_before_the_first_paragraph_is_the_very_start() -> None:
    assert cursor_from_offset("\n\nOne.\n", 1) == Cursor(para=0, offset=0)


def test_cursor_from_offset_on_a_blank_only_body_is_the_origin() -> None:
    assert cursor_from_offset("\n\n\n", 1) == Cursor(para=0, offset=0)


# --- assemble with a cursor ---------------------------------------------------


def test_assemble_with_no_cursor_is_a_continuation() -> None:
    body = "One.\n\nTwo.\n\nThree.\n\nFour.\n"
    context = assemble(body, budget=20)
    assert context.prefix == trim_to_tail(body, 20)
    assert context.suffix == ""


def test_a_cursor_at_the_very_end_degenerates_to_no_cursor_at_all() -> None:
    """Caret at the end of the file means an empty suffix (#14) -- byte-identical to
    not reporting a caret, not a fill-in-the-middle prompt with nothing to fill."""
    body = "One.\n\nTwo.\n\nThree.\n\nFour.\n"
    paragraphs, _ = provenance.split_paragraphs(body)
    end = Cursor(para=len(paragraphs) - 1, offset=len(paragraphs[-1]))
    context = assemble(body, budget=20, cursor=end)
    assert context == assemble(body, budget=20)


def test_a_cursor_mid_file_reconstructs_the_body_around_it_under_budget() -> None:
    body = "One.\n\nTwo.\n\nThree.\n\nFour.\n"
    context = assemble(body, budget=1000, cursor=Cursor(para=1, offset=2))
    assert context.prefix == "One.\n\nTw"
    assert context.suffix == "o.\n\nThree.\n\nFour.\n"


def test_the_whole_budget_goes_to_the_prefix_when_the_suffix_is_empty() -> None:
    body = "One.\n\nTwo.\n"
    context = assemble(body, budget=5, cursor=Cursor(para=1, offset=4), prefix_share=0.1)
    assert context.prefix == trim_to_tail(body, 5)
    assert context.suffix == ""


def test_the_budget_splits_by_prefix_share_once_there_is_a_suffix() -> None:
    body = "One.\n\nTwo.\n"
    context = assemble(body, budget=10, cursor=Cursor(para=1, offset=2), prefix_share=0.75)
    # prefix_budget = floor(10 * 0.75) = 7, suffix_budget = 3.
    assert context.prefix == trim_to_tail("One.\n\nTw", 7)
    assert context.suffix == trim_to_head("o.\n", 3)


def test_assemble_rejects_a_cursor_out_of_range() -> None:
    with pytest.raises(CursorOutOfRange):
        assemble("One.\n\nTwo.\n", budget=100, cursor=Cursor(para=99, offset=0))


def test_assemble_rejects_any_cursor_on_an_empty_body() -> None:
    with pytest.raises(CursorOutOfRange):
        assemble("", budget=100, cursor=Cursor(para=0, offset=0))


# --- word counting -------------------------------------------------------------


def test_count_words_splits_on_whitespace() -> None:
    assert count_words("He was angry. Very angry.") == 5


def test_count_words_of_an_empty_string_is_zero() -> None:
    assert count_words("") == 0


def test_count_words_ignores_runs_of_whitespace() -> None:
    assert count_words("One   Two\n\nThree") == 3


# --- the selection -------------------------------------------------------------


def test_split_at_range_within_one_paragraph() -> None:
    paras, seps = provenance.split_paragraphs("One two three.\n\nFour.\n")
    prefix, selection, suffix = split_at_range(
        paras, seps, CursorRange(Cursor(para=0, offset=4), Cursor(para=0, offset=7))
    )
    assert provenance.join_paragraphs(*prefix) == "One "
    assert provenance.join_paragraphs(*selection) == "two"
    assert provenance.join_paragraphs(*suffix) == " three.\n\nFour.\n"


def test_split_at_range_across_several_paragraphs() -> None:
    paras, seps = provenance.split_paragraphs("One.\n\nTwo.\n\nThree.\n\nFour.\n")
    prefix, selection, suffix = split_at_range(
        paras, seps, CursorRange(Cursor(para=0, offset=2), Cursor(para=2, offset=2))
    )
    assert provenance.join_paragraphs(*prefix) == "On"
    assert provenance.join_paragraphs(*selection) == "e.\n\nTwo.\n\nTh"
    assert provenance.join_paragraphs(*suffix) == "ree.\n\nFour.\n"


def test_split_at_range_rejects_an_inverted_pair() -> None:
    paras, seps = provenance.split_paragraphs("One.\n\nTwo.\n")
    with pytest.raises(InvertedRange):
        split_at_range(paras, seps, CursorRange(Cursor(para=1, offset=0), Cursor(para=0, offset=0)))


def test_split_at_range_rejects_an_out_of_range_end() -> None:
    paras, seps = provenance.split_paragraphs("One.\n\nTwo.\n")
    with pytest.raises(CursorOutOfRange):
        split_at_range(paras, seps, CursorRange(Cursor(para=0, offset=0), Cursor(para=99, offset=0)))


def test_a_collapsed_selection_is_byte_identical_to_the_same_cursor() -> None:
    body = "One.\n\nTwo.\n\nThree.\n"
    cursor = Cursor(para=1, offset=2)
    by_cursor = assemble(body, budget=1000, cursor=cursor)
    by_selection = assemble(body, budget=1000, selection=CursorRange(cursor, cursor))
    assert by_cursor == by_selection


def test_a_selection_mid_file_puts_text_either_side_of_it() -> None:
    body = "One.\n\nTwo.\n\nThree.\n\nFour.\n"
    context = assemble(
        body,
        budget=1000,
        selection=CursorRange(Cursor(para=1, offset=0), Cursor(para=1, offset=4)),
    )
    assert context.prefix == "One.\n\n"
    assert context.suffix == "\n\nThree.\n\nFour.\n"
    assert context.selection_words == 1


def test_the_selected_text_itself_is_never_in_the_context() -> None:
    body = "One.\n\nSecretWord.\n\nThree.\n"
    context = assemble(
        body,
        budget=1000,
        selection=CursorRange(Cursor(para=1, offset=0), Cursor(para=1, offset=11)),
    )
    assert "SecretWord" not in context.prefix
    assert "SecretWord" not in context.suffix


def test_a_selection_reaching_the_end_of_the_file_gives_an_empty_suffix() -> None:
    """The same end-of-file bug #14 fixed for a caret, in a second place: the body's
    own closing separator must not be handed to the suffix."""
    body = "One.\n\nTwo.\n"
    paragraphs, _ = provenance.split_paragraphs(body)
    end = Cursor(para=len(paragraphs) - 1, offset=len(paragraphs[-1]))
    context = assemble(
        body, budget=1000, selection=CursorRange(Cursor(para=0, offset=0), end)
    )
    assert context.suffix == ""
    assert context.selection_words == 2


def test_a_selection_starting_at_the_very_beginning_gives_an_empty_prefix() -> None:
    body = "One.\n\nTwo.\n"
    context = assemble(
        body,
        budget=1000,
        selection=CursorRange(Cursor(para=0, offset=0), Cursor(para=1, offset=0)),
    )
    assert context.prefix == ""


def test_a_whitespace_only_selection_degenerates_to_the_caret_at_its_start() -> None:
    body = "One.\n\nTwo.\n"
    start = Cursor(para=0, offset=4)  # end of "One."
    end = Cursor(para=1, offset=0)  # start of "Two." -- nothing but the separator between
    by_selection = assemble(body, budget=1000, selection=CursorRange(start, end))
    by_cursor = assemble(body, budget=1000, cursor=start)
    assert by_selection == by_cursor
    assert by_selection.selection_words is None


def test_the_budget_split_is_unaffected_by_a_selection_being_present() -> None:
    body = "One.\n\nTwo.\n\nThree.\n"
    cursor = Cursor(para=1, offset=2)
    by_cursor = assemble(body, budget=10, cursor=cursor, prefix_share=0.75)
    by_selection = assemble(
        body, budget=10, selection=CursorRange(cursor, cursor), prefix_share=0.75
    )
    assert by_cursor.prefix == by_selection.prefix
    assert by_cursor.suffix == by_selection.suffix


def test_assemble_rejects_both_a_cursor_and_a_selection() -> None:
    body = "One.\n\nTwo.\n"
    cursor = Cursor(para=0, offset=0)
    with pytest.raises(ValueError):
        assemble(body, budget=100, cursor=cursor, selection=CursorRange(cursor, cursor))


def test_assemble_rejects_an_inverted_selection() -> None:
    body = "One.\n\nTwo.\n"
    with pytest.raises(InvertedRange):
        assemble(
            body,
            budget=100,
            selection=CursorRange(Cursor(para=1, offset=0), Cursor(para=0, offset=0)),
        )


def test_send_selection_defaults_to_off() -> None:
    """`assemble`'s own default is conservative; the server's (`llm.py`'s
    `GenerationOptions`) is the one that is on."""
    body = "One.\n\nSecretWord.\n\nThree.\n"
    context = assemble(
        body,
        budget=1000,
        selection=CursorRange(Cursor(para=1, offset=0), Cursor(para=1, offset=11)),
    )
    assert context.selection is None
    assert context.selection_words == 1


def test_send_selection_true_carries_the_passage_text() -> None:
    body = "One.\n\nSecretWord.\n\nThree.\n"
    context = assemble(
        body,
        budget=1000,
        selection=CursorRange(Cursor(para=1, offset=0), Cursor(para=1, offset=11)),
        send_selection=True,
    )
    assert context.selection == "SecretWord."
    assert context.selection_words == 1


def test_send_selection_true_with_no_selection_changes_nothing() -> None:
    """The flag only ever matters once there is a real selection -- a continuation or
    a fill-in-the-middle has no passage to show."""
    body = "One.\n\nTwo.\n\nThree.\n"
    cursor = Cursor(para=1, offset=2)
    without = assemble(body, budget=1000, cursor=cursor, send_selection=False)
    with_flag = assemble(body, budget=1000, cursor=cursor, send_selection=True)
    assert without == with_flag


def test_the_passage_is_charged_against_the_budget_before_the_sides_split() -> None:
    """Decision taken when extending #20: a shown passage costs budget, so the prefix
    and the suffix are shorter than the same call with it withheld."""
    body = "One two three four.\n\nFive six seven eight nine ten.\n\nEleven twelve.\n"
    selection = CursorRange(Cursor(para=1, offset=0), Cursor(para=1, offset=len(
        "Five six seven eight nine ten."
    )))
    withheld = assemble(body, budget=20, selection=selection, send_selection=False)
    shown = assemble(body, budget=20, selection=selection, send_selection=True)
    assert len(shown.prefix) + len(shown.suffix) < len(withheld.prefix) + len(withheld.suffix)


def test_a_passage_larger_than_the_budget_leaves_both_sides_with_no_real_content() -> None:
    """No ceiling on the passage's own size (decided against building one): charging
    it against the budget first can leave nothing but trimming residue for either
    side -- a trailing or leading separator, never the writer's own words -- rather
    than trimming the passage itself to make room."""
    body = "Short.\n\nA much, much longer passage than the tiny budget allows for.\n\nEnd.\n"
    selection = CursorRange(
        Cursor(para=1, offset=0),
        Cursor(para=1, offset=len("A much, much longer passage than the tiny budget allows for.")),
    )
    context = assemble(body, budget=5, selection=selection, send_selection=True)
    assert context.prefix.strip() == ""
    assert context.suffix.strip() == ""
    assert context.selection is not None


# --- the synopsis (api#17) ----------------------------------------------------


def test_synopsis_defaults_to_empty() -> None:
    assert assemble("hello", budget=10).synopsis == ""


def test_synopsis_passes_straight_through() -> None:
    context = assemble("hello", budget=10, synopsis="A letter nobody has opened in three years.")
    assert context.synopsis == "A letter nobody has opened in three years."


def test_synopsis_is_never_trimmed_or_charged_against_the_budget() -> None:
    """Deliberate: short by construction at the source (the frontend's own
    MAX_SUMMARY_LENGTH), unlike the prefix/suffix/selection."""
    long_synopsis = "Word " * 1000
    context = assemble("hello", budget=10, synopsis=long_synopsis)
    assert context.synopsis == long_synopsis
