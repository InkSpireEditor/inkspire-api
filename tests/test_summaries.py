# -*- coding: utf-8 -*-
"""`assemble_summary`, `clean_summary`, and `update` -- the background call (api#18).

The route-level behaviour (what a save schedules, and when) is in
`test_documents.py`; this file is about the pieces `update` is built from, and about
`update` itself against a scanner and a mocked provider, without going through a
request at all.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from tests.conftest import answer_body, llm_service, make_story

from inkspire_api import context_summary, summaries
from inkspire_api.context_summary import parse_section
from inkspire_api.llm import LLMError
from inkspire_api.storage import Scanner

_PROMPTS = json.loads((Path(__file__).parent / "data" / "prompts.json").read_text(encoding="utf-8"))
CONTEXT_SUMMARY_PROMPTS: dict[str, dict[str, str]] = _PROMPTS["context_summary"]


# --- the exact prompt sent, pinned alongside every other shape (test_llm.py) -----


@pytest.mark.parametrize("case", sorted(CONTEXT_SUMMARY_PROMPTS))
def test_the_summary_prompt_is_rendered_exactly_as_recorded(case: str) -> None:
    recorded = CONTEXT_SUMMARY_PROMPTS[case]
    context = summaries.assemble_summary(recorded["dropped"], budget=10_000)
    assert summaries.render_summary(context) == recorded["prompt"]


# --- assemble_summary: the head, not the tail -------------------------------


def test_assemble_summary_keeps_the_head_not_the_tail() -> None:
    dropped = ("Jane Doe is the narrator. " * 200) + "The last line."
    context = summaries.assemble_summary(dropped, budget=50)
    assert context.dropped.startswith("Jane Doe")
    assert "The last line." not in context.dropped


def test_assemble_summary_keeps_everything_under_budget() -> None:
    dropped = "Short opening."
    assert summaries.assemble_summary(dropped, budget=10_000).dropped == dropped


# --- clean_summary ------------------------------------------------------------


def test_clean_summary_collapses_internal_blank_lines() -> None:
    raw = "Jane Doe is the narrator.\n\nThe house is her late aunt's."
    assert summaries.clean_summary(raw) == "Jane Doe is the narrator. The house is her late aunt's."


def test_clean_summary_strips_surrounding_whitespace() -> None:
    assert summaries.clean_summary("  Jane Doe is the narrator.  \n") == "Jane Doe is the narrator."


def test_clean_summary_caps_at_the_length_limit() -> None:
    raw = "x " * 10_000
    cleaned = summaries.clean_summary(raw)
    assert len(cleaned) == summaries.MAX_SUMMARY_CHARS


def test_clean_summary_of_nothing_but_whitespace_is_empty() -> None:
    assert summaries.clean_summary("   \n\n   ") == ""


# --- resummarise: the decision sequence `update` and `inkspire ink maintenance` ---
# --- share (api#27) -----------------------------------------------------------

_LONG_BODY = ("Jane Doe is the narrator. " * 400) + "\n\nThe end."


def _resummarise(body, stored_section, *, tmp_path, handler, budget=50, respect_trigger=True):
    service = llm_service(tmp_path, handler)
    return asyncio.run(
        summaries.resummarise(
            body,
            stored_section,
            service=service,
            model="p/model",
            budget=budget,
            respect_trigger=respect_trigger,
        )
    )


def test_resummarise_answers_untrimmed_when_nothing_is_dropped(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no model call was expected")

    outcome = _resummarise("Short.", None, tmp_path=tmp_path, handler=handler, budget=10_000)

    assert outcome.state == summaries.UNTRIMMED
    assert outcome.dropped_chars == 0
    assert outcome.section is None


def test_resummarise_answers_current_when_not_due(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no model call was expected")

    stored = context_summary.render_section(
        context_summary.ContextSummary(text="Already covered.", dropped_chars=len(_LONG_BODY))
    )

    outcome = _resummarise(_LONG_BODY, stored, tmp_path=tmp_path, handler=handler)

    assert outcome.state == summaries.CURRENT
    assert outcome.section is None


def test_resummarise_ignores_the_trigger_when_told_to(tmp_path: Path) -> None:
    """The one thing `inkspire ink maintenance` needs that `update` does not: a
    summary `is_due` calls current still gets recomputed when asked."""
    stored = context_summary.render_section(
        context_summary.ContextSummary(text="Already covered.", dropped_chars=len(_LONG_BODY))
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=answer_body("Jane Doe is the narrator."))

    outcome = _resummarise(
        _LONG_BODY, stored, tmp_path=tmp_path, handler=handler, respect_trigger=False
    )

    assert outcome.state == summaries.WRITTEN
    assert outcome.section is not None


def test_resummarise_answers_unusable_for_nothing_worth_storing(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=answer_body("   "))

    outcome = _resummarise(_LONG_BODY, None, tmp_path=tmp_path, handler=handler)

    assert outcome.state == summaries.UNUSABLE
    assert outcome.section is None


def test_resummarise_raises_rather_than_swallowing_a_provider_failure(tmp_path: Path) -> None:
    """Unlike `update`, which logs and returns: a caller running this by hand wants
    to see the failure, not have it disappear into a log nobody is watching."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    with pytest.raises(LLMError):
        _resummarise(_LONG_BODY, None, tmp_path=tmp_path, handler=handler)


# --- update: the background task, against a real scanner --------------------


@pytest.fixture
def scanner(data_root: Path) -> Scanner:
    return Scanner(data_root)


def _update(scanner: Scanner, file_id: str, *, tmp_path: Path, handler, budget: int = 50) -> None:
    """Runs `summaries.update` to completion against a mocked provider."""
    service = llm_service(tmp_path, handler)
    asyncio.run(summaries.update(scanner, file_id, service=service, model="p/model", budget=budget))


def _one_chapter(data_root: Path, body: str) -> tuple[Scanner, str]:
    make_story(data_root, "example-story", chapters={"first.ink": body})
    scanner = Scanner(data_root)
    return scanner, next(iter(scanner.tree().chapters))


def test_update_writes_a_section_when_the_body_is_over_budget_and_due(
    data_root: Path, tmp_path: Path
) -> None:
    scanner, file_id = _one_chapter(data_root, _LONG_BODY)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=answer_body("Jane Doe is the narrator."))

    _update(scanner, file_id, tmp_path=tmp_path, handler=handler)

    document = scanner.read_document(file_id)
    assert document.context_summary is not None
    parsed = parse_section(document.context_summary)
    assert parsed is not None
    assert parsed.text == "Jane Doe is the narrator."


def test_update_writes_nothing_when_the_body_is_under_budget(
    data_root: Path, tmp_path: Path
) -> None:
    scanner, file_id = _one_chapter(data_root, "Short.")
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, text=answer_body("Should not be asked."))

    _update(scanner, file_id, tmp_path=tmp_path, handler=handler, budget=10_000)

    assert not called
    assert scanner.read_document(file_id).context_summary is None


def test_update_skips_a_call_not_yet_due(data_root: Path, tmp_path: Path) -> None:
    """A second update, right after the first, with barely any change in what is
    dropped, must not spend a second model call."""
    scanner, file_id = _one_chapter(data_root, _LONG_BODY)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=answer_body("Jane Doe is the narrator."))

    _update(scanner, file_id, tmp_path=tmp_path, handler=handler)
    assert len(calls) == 1

    _update(scanner, file_id, tmp_path=tmp_path, handler=handler)
    assert len(calls) == 1  # unchanged: the second call was skipped


def test_update_leaves_a_mismatched_body_unwritten(data_root: Path, tmp_path: Path) -> None:
    """The writer saved again while the model was answering -- the summary that
    comes back describes text that has already moved, so it must not be written."""
    scanner, file_id = _one_chapter(data_root, _LONG_BODY)

    def handler(_request: httpx.Request) -> httpx.Response:
        # Simulate a save landing while this call is "in flight" by changing the
        # file on disk before the (synchronous, in this test) call returns.
        scanner.write_document(file_id, "A completely different body.", None)
        return httpx.Response(200, text=answer_body("Jane Doe is the narrator."))

    _update(scanner, file_id, tmp_path=tmp_path, handler=handler)

    assert scanner.read_document(file_id).context_summary is None


def test_update_swallows_a_provider_failure(data_root: Path, tmp_path: Path) -> None:
    scanner, file_id = _one_chapter(data_root, _LONG_BODY)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _update(scanner, file_id, tmp_path=tmp_path, handler=handler)  # must not raise

    assert scanner.read_document(file_id).context_summary is None


def test_update_writes_nothing_for_a_model_answer_with_nothing_usable(
    data_root: Path, tmp_path: Path
) -> None:
    scanner, file_id = _one_chapter(data_root, _LONG_BODY)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=answer_body("   "))

    _update(scanner, file_id, tmp_path=tmp_path, handler=handler)

    assert scanner.read_document(file_id).context_summary is None
