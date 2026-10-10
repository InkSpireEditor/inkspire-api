# -*- coding: utf-8 -*-
"""The `ink:context_summary` section's format, and the trigger metric (api#18)."""

from __future__ import annotations

from inkspire_api.context_summary import (
    GROWTH_FACTOR,
    MIN_TRIGGER_CHARS,
    ContextSummary,
    dropped,
    is_due,
    parse_section,
    render_section,
)

# --- is_due: the trigger metric ---------------------------------------------


def test_nothing_stored_and_nothing_dropped_is_not_due() -> None:
    assert is_due(0, 0) is False


def test_a_first_trim_under_the_floor_is_not_due() -> None:
    assert is_due(MIN_TRIGGER_CHARS - 1, 0) is False


def test_a_first_trim_at_the_floor_is_due() -> None:
    assert is_due(MIN_TRIGGER_CHARS, 0) is True


def test_growth_under_the_doubling_threshold_is_not_due() -> None:
    stored = 4000
    assert is_due(stored + int(stored * (GROWTH_FACTOR - 1)) - 1, stored) is False


def test_growth_at_the_doubling_threshold_is_due() -> None:
    stored = 4000
    assert is_due(stored + int(stored * (GROWTH_FACTOR - 1)), stored) is True


def test_the_ladder_roughly_doubles_each_time() -> None:
    """2k, 4k, 8k, 16k -- the issue's own worked example."""
    assert is_due(2000, 0) is True
    assert is_due(3999, 2000) is False
    assert is_due(4000, 2000) is True
    assert is_due(7999, 4000) is False
    assert is_due(8000, 4000) is True


def test_a_small_change_once_a_lot_is_already_dropped_is_not_due() -> None:
    """The floor alone would fire here; proportional growth is what holds it back."""
    assert is_due(100_000 + MIN_TRIGGER_CHARS, 100_000) is False


def test_a_large_deletion_is_due_the_same_as_a_large_growth() -> None:
    """Symmetric on purpose: a summary describing paragraphs the writer has since
    cut must not stand forever just because the count went down instead of up."""
    assert is_due(0, 8000) is True


def test_a_small_deletion_is_not_due() -> None:
    assert is_due(7999, 8000) is False


# --- dropped: what a continuation's own trim would cut ----------------------


def test_a_body_under_budget_drops_nothing() -> None:
    assert dropped("Short.", budget=1000) == ""


def test_a_body_over_budget_drops_its_opening() -> None:
    body = ("Jane Doe is the narrator.\n\n" * 50) + "The end is near."
    result = dropped(body, budget=20)
    assert result != ""
    assert result + body[len(result) :] == body


def test_a_single_paragraph_over_budget_drops_from_the_ladder() -> None:
    """One paragraph alone exceeding the budget still falls on the dropped side,
    through `trim_to_tail`'s own single-paragraph ladder."""
    body = "x" * 500
    result = dropped(body, budget=100)
    assert result != ""
    assert result + body[len(result) :] == body


def test_a_body_of_only_separators_drops_the_excess() -> None:
    body = "\n\n\n\n\n\n\n\n\n\n"
    result = dropped(body, budget=3)
    assert result + body[len(result) :] == body


# --- render_section / parse_section: the round trip --------------------------


def test_a_summary_round_trips_through_the_section_text() -> None:
    summary = ContextSummary(text="Jane Doe is the narrator.", dropped_chars=14207)
    parsed = parse_section(render_section(summary))
    assert parsed == summary


def test_the_section_is_valid_yaml_with_the_key_as_a_block() -> None:
    summary = ContextSummary(text="Line one.\nLine two.", dropped_chars=42)
    text = render_section(summary)
    assert "summary: |" in text
    assert "dropped_chars: 42" in text


def test_text_that_is_not_yaml_answers_none() -> None:
    assert parse_section("not: valid: yaml: at: all:") is None


def test_text_that_is_not_a_mapping_answers_none() -> None:
    assert parse_section("- just\n- a\n- list\n") is None


def test_a_mapping_missing_the_summary_key_answers_none() -> None:
    assert parse_section("dropped_chars: 10\n") is None


def test_a_mapping_missing_the_dropped_chars_key_answers_none() -> None:
    assert parse_section("summary: hello\n") is None


def test_a_non_string_summary_answers_none() -> None:
    assert parse_section("summary: 123\ndropped_chars: 10\n") is None


def test_a_non_integer_dropped_chars_answers_none() -> None:
    assert parse_section("summary: hello\ndropped_chars: not-a-number\n") is None
