# -*- coding: utf-8 -*-
"""Summarising what a generation's prompt budget last trimmed off a file (api#18).

`trim_to_tail` (`prompt.py`) silently drops whatever does not fit the prompt budget, so a
long chapter generates against its last `INKSPIRE_LLM_PROMPT_BUDGET` characters with no
trace of who was established on its first page. This module computes, in the background
on save, one or two sentences summarising exactly the paragraphs a trim would drop, stores
them in the file's own `ink:context_summary` section (`context_summary.py` has the format
and the trigger metric), and `prompt.py`'s `assemble` renders them back into the prompt --
but only when *that* request's own trim actually cut the prefix
(`PromptContext.summary`'s own docstring has why).

Distinct from two things that might sound similar: `prompt.py`'s `synopsis` field (api#17)
is independent of the trim boundary and renders whatever the budget did; api#15's
retrieval recalls *external* lore the prose evokes without naming. This is neither -- it
is keyed specifically on what one trim just removed from this file's own prose.

Reads `INKSPIRE_LLM_SMALL_MODEL`, the same setting `titles.py`'s proposed-title route
reads -- one knob for every short, non-generation call this server makes, not one per
feature. Unset, nothing here runs at all (`documents.py`'s `_write` never schedules the
background task).

Triggered from `documents.py`'s `_write`, on every save, through `update()` below -- a
`BackgroundTasks.add_task`, so the save itself returns before a summary call starts and a
writer never waits on one. Best-effort: a save landing while a previous summary call for
the same file is still in flight is skipped, not queued, so the summary may sit one save
behind until the next one catches up -- accepted, the same way `repository.reconcile`
elsewhere in this codebase accepts "good enough" over "exactly right."
"""

from __future__ import annotations

import dataclasses
import logging

from . import prompt as prompt_lib
from .context_summary import ContextSummary, dropped, is_due, parse_section, render_section
from .fs import StorageError
from .llm import LLMError, LLMService, UnknownModel
from .notes import NotesScanner
from .storage import Scanner

logger = logging.getLogger(__name__)

SUMMARY_TEMPLATE = "summary.j2"

#: Longest a stored summary may be. One or two sentences, not a second copy of the
#: region it describes.
MAX_SUMMARY_CHARS = 1000


@dataclasses.dataclass(frozen=True)
class SummaryContext:
    """What `summary.j2` can render: the dropped region's own text."""

    dropped: str


def assemble_summary(text: str, *, budget: int) -> SummaryContext:
    """`text`'s opening within `budget` characters, cut at a paragraph boundary.

    The head, not the tail `prompt_lib.trim_to_tail` keeps for a continuation's own
    prefix -- the same reasoning `titles.py`'s `assemble_title` already uses: what
    opens a dropped region is what establishes the names and facts worth keeping, so
    a dropped region larger than `budget` loses its middle rather than its start. A
    rolling summary (api#14) is the fix for that; this is not it.
    """
    return SummaryContext(dropped=prompt_lib.trim_to_head(text, budget))


def render_summary(context: SummaryContext) -> str:
    """Wraps `context` in the summary instructions sent to the model."""
    return prompt_lib.JINJA.get_template(SUMMARY_TEMPLATE).render(ctx=context)


def clean_summary(raw: str) -> str:
    """`raw`, a small model's answer, made fit to store.

    Collapses blank lines -- a model asked for "one or two sentences" sometimes
    answers with a paragraph break in the middle of them anyway -- and caps the
    result at `MAX_SUMMARY_CHARS`. `""` for nothing usable, which the caller treats
    as "write nothing": there is no request waiting on this to answer anything.
    """
    collapsed = " ".join(raw.split())
    return collapsed[:MAX_SUMMARY_CHARS]


async def update(
    scanner: Scanner | NotesScanner,
    file_id: str,
    *,
    service: LLMService,
    model: str,
    budget: int,
) -> None:
    """The background task: summarises what a trim of `file_id`'s body at `budget`
    characters would drop, if a new summary is due, and writes the result.

    Scheduled from `documents.py`'s `_write` after every save, and only when `model`
    (`INKSPIRE_LLM_SMALL_MODEL`) is configured. Every failure -- the file
    disappearing under it, the provider being unreachable, the model answering
    nothing usable -- is logged and swallowed rather than raised: this runs after the
    save that triggered it has already answered 200, and nothing here may surface on
    it.
    """
    try:
        document = scanner.read_document(file_id)
    except StorageError:
        logger.exception("could not read %s for a context summary", file_id)
        return

    dropped_text = dropped(document.body, budget)
    dropped_chars = len(dropped_text)
    if dropped_chars == 0:
        # Nothing is being trimmed any more. Leaving a stale section in place is
        # harmless -- prompt.assemble never renders a summary once nothing was
        # trimmed -- and overwriting it would spend a model call on an answer
        # nobody reads.
        return

    stored = parse_section(document.context_summary) if document.context_summary else None
    if not is_due(dropped_chars, stored.dropped_chars if stored is not None else 0):
        return

    try:
        rendered = render_summary(assemble_summary(dropped_text, budget=budget))
        raw = await service.complete(
            model, rendered, options=service.defaults.overlay(think=False)
        )
    except (LLMError, UnknownModel):
        logger.exception("the context-summary call failed for %s", file_id)
        return

    cleaned = clean_summary(raw)
    if not cleaned:
        return

    section = render_section(ContextSummary(text=cleaned, dropped_chars=dropped_chars))
    try:
        scanner.write_context_summary(file_id, document.body, section)
    except StorageError:
        logger.exception("could not write the context summary for %s", file_id)
