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

`update`'s own decision sequence and model call -- is anything dropped, is a new call due,
ask, clean -- is `resummarise`, below, shared with `inkspire ink maintenance`
(`cli.py`, api#27): that command cannot call `update` itself, since `update` resolves a
file by id through a `Scanner`, which skips a story directory with no `story.yaml`, while
the command walks paths `rglob` finds regardless; and `update` is silent by construction,
every no-op and failure a bare `return`, which a command report needs to tell apart.
`resummarise` reads and writes nothing -- a `body` and a stored section's text in,
an `Outcome` out -- so `update` is the only thing here that touches a scanner, and the
command the only thing that touches a path.
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

#: What `resummarise` decided, named so a caller's report does not have to infer it back
#: from which field of `Outcome` is set.
#:
#: UNTRIMMED: nothing is dropped at this budget, so there is nothing to summarise.
#: CURRENT: something is dropped, but `is_due` says the stored summary still covers it
#:          closely enough -- never reached with `respect_trigger=False`.
#: UNUSABLE: the model answered, but `clean_summary` found nothing worth storing.
#: WRITTEN: `outcome.section` holds a fresh section's text, ready to write.
UNTRIMMED, CURRENT, UNUSABLE, WRITTEN = "untrimmed", "current", "unusable", "written"


@dataclasses.dataclass(frozen=True)
class Outcome:
    """What one call to `resummarise` decided.

    `section` is the rendered `ink:context_summary` text to write, present only for
    `WRITTEN`. `dropped_chars` is reported regardless of `state` -- even `UNTRIMMED`'s
    `0` is informative, and the other three states are exactly what a caller would
    otherwise have to recompute via `dropped()` just to report.
    """

    state: str
    dropped_chars: int
    section: str | None = None


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


async def resummarise(
    body: str,
    stored_section: str | None,
    *,
    service: LLMService,
    model: str,
    budget: int,
    respect_trigger: bool = True,
) -> Outcome:
    """Decides whether `body` needs a fresh `ink:context_summary` at `budget`
    characters and, where it does, asks for and cleans one.

    `respect_trigger` is `is_due`'s own gate: `True` for `update`, which must not spend
    a call every single save; `False` for `inkspire ink maintenance`, which exists
    specifically to resummarise everything a size-based trigger cannot tell is stale --
    docs/prompt.md's own "hidden manual trigger" entry.

    Reads and writes nothing; a caller with a scanner (`update`) or a path
    (`cli.maintain_one`) does that with the `Outcome` this returns. Raises
    `LLMError`/`UnknownModel` rather than swallowing them, unlike `update` itself --
    a caller running by hand wants to see a provider failure, not have it logged where
    nothing is watching.
    """
    dropped_text = dropped(body, budget)
    dropped_chars = len(dropped_text)
    if dropped_chars == 0:
        # Nothing is being trimmed any more. Leaving a stale section in place is
        # harmless -- prompt.assemble never renders a summary once nothing was
        # trimmed -- and resummarising would spend a model call on an answer nobody
        # reads.
        return Outcome(UNTRIMMED, dropped_chars)

    if respect_trigger:
        stored = parse_section(stored_section) if stored_section else None
        if not is_due(dropped_chars, stored.dropped_chars if stored is not None else 0):
            return Outcome(CURRENT, dropped_chars)

    rendered = render_summary(assemble_summary(dropped_text, budget=budget))
    raw = await service.complete(model, rendered, options=service.defaults.overlay(think=False))

    cleaned = clean_summary(raw)
    if not cleaned:
        return Outcome(UNUSABLE, dropped_chars)

    section = render_section(ContextSummary(text=cleaned, dropped_chars=dropped_chars))
    return Outcome(WRITTEN, dropped_chars, section)


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

    try:
        outcome = await resummarise(
            document.body, document.context_summary, service=service, model=model, budget=budget
        )
    except (LLMError, UnknownModel):
        logger.exception("the context-summary call failed for %s", file_id)
        return

    if outcome.section is None:
        return

    try:
        scanner.write_context_summary(file_id, document.body, outcome.section)
    except StorageError:
        logger.exception("could not write the context summary for %s", file_id)
