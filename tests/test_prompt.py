# -*- coding: utf-8 -*-
"""`trim_to_tail`, the one piece of policy in `inkspire_api/prompt.py`.

The render itself is pinned character for character in `tests/test_llm.py`, against
`tests/data/prompts.json`; these tests are about what reaches the render, not what it
produces.
"""

from __future__ import annotations

import pytest

from inkspire_api.prompt import PromptContext, assemble, render, trim_to_tail


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
    assert assemble("hello", budget=10) == PromptContext(text="hello")
    assert assemble("hello world, this is long", budget=5) != PromptContext(
        text="hello world, this is long"
    )


def test_render_ends_on_the_contexts_last_character() -> None:
    assert render(PromptContext(text="the house was")).endswith("the house was")
