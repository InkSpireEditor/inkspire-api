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
from collections.abc import Collection

from . import prompt as prompt_lib
from .context_summary import ContextSummary, dropped, is_due, parse_section, render_section
from .fs import StorageError
from .llm import LLMError, LLMService, RefusedProvider, UnknownModel
from .notes import NotesScanner
from .storage import Scanner

logger = logging.getLogger(__name__)

SUMMARY_TEMPLATE = "summary.j2"

#: Longest a stored summary may be. One or two sentences, not a second copy of the
#: region it describes.
MAX_SUMMARY_CHARS = 1000

#: The character-per-token assumption `docs/prompt.md` already names and accepts for
#: the writer's own budget; used the same way here to turn a token-denominated
#: `INKSPIRE_LLM_SUMMARY_NUM_CTX` into a character budget for `assemble_summary`.
CHARS_PER_TOKEN = 4

#: Tokens reserved out of the summary model's own window for `summary.j2`'s fixed
#: instructions plus margin -- the dropped region's own budget is what remains.
RESERVED_TOKENS = 768

#: The summary call's own answer cap, passed as `complete`'s `max_output_tokens` --
#: `num_predict` on an Ollama provider, `max_tokens` on a chat-completions one.
#: `clean_summary`'s `MAX_SUMMARY_CHARS` is a second, character-denominated brake on
#: the same answer; this one bounds what the provider itself is asked to produce.
SUMMARY_OUTPUT_TOKENS = 400

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


def summary_budget(summary_num_ctx: int) -> int:
    """How many characters of a dropped region one summary call can read whole, given
    a small model whose own context window is `summary_num_ctx` tokens.

    `RESERVED_TOKENS` is held back for `summary.j2`'s fixed instructions and the
    answer itself; what remains converts to characters at `CHARS_PER_TOKEN`, floored
    at `0` for a window so small that nothing is left (`settings.py`'s own `ge=1024`
    keeps this from biting in practice, but a caller should not have to trust that).
    """
    return max((summary_num_ctx - RESERVED_TOKENS) * CHARS_PER_TOKEN, 0)


def assemble_summary(text: str, *, budget: int) -> SummaryContext:
    """`text` within `budget` characters, cut at a paragraph boundary.

    `budget` is normally `summary_budget(summary_num_ctx)` -- large enough that a
    realistic chapter's whole dropped region fits, so this is the overflow guard
    rather than the normal path. Where it still does not fit, the head is kept, not
    the tail `prompt_lib.trim_to_tail` keeps for a continuation's own prefix -- the
    same reasoning `titles.py`'s `assemble_title` already uses: what opens a dropped
    region is what establishes the names and facts worth keeping. A region larger
    than `summary_num_ctx` can still lose its middle this way; api#28 is where that
    residual case lives.
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
    prompt_budget: int,
    summary_num_ctx: int,
    tags: Collection[str],
    respect_trigger: bool = True,
) -> Outcome:
    """Decides whether `body` needs a fresh `ink:context_summary` at `prompt_budget`
    characters and, where it does, asks for and cleans one.

    `prompt_budget` and `summary_num_ctx` answer two different questions that used to
    be one parameter: `prompt_budget` is the *writer's* budget, which is what decides
    what a generation's own trim would drop (`dropped`, below) -- unrelated to the
    summary model. `summary_num_ctx` is the *small* model's own context window, which
    `summary_budget` turns into how much of that dropped region one call can read
    (api#28 -- the same number, reused as this call's own `num_ctx`, so the budget and
    the window can never disagree).

    `respect_trigger` is `is_due`'s own gate: `True` for `update`, which must not spend
    a call every single save; `False` for `inkspire ink maintenance`, which exists
    specifically to resummarise everything a size-based trigger cannot tell is stale --
    docs/prompt.md's own "hidden manual trigger" entry.

    `tags` are the file's own: a `model` whose provider refuses one of them is not
    called, and `RefusedProvider` is raised instead.

    Reads and writes nothing; a caller with a scanner (`update`) or a path
    (`cli.maintain_one`) does that with the `Outcome` this returns. Raises
    `LLMError`/`UnknownModel`/`RefusedProvider` rather than swallowing them, unlike `update` itself --
    a caller running by hand wants to see a provider failure, not have it logged where
    nothing is watching.
    """
    dropped_text = dropped(body, prompt_budget)
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

    rendered = render_summary(assemble_summary(dropped_text, budget=summary_budget(summary_num_ctx)))
    raw = await service.complete(
        model,
        rendered,
        tags=tags,
        options=service.defaults.overlay(think=False, num_ctx=summary_num_ctx),
        max_output_tokens=SUMMARY_OUTPUT_TOKENS,
    )

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
    prompt_budget: int,
    summary_num_ctx: int,
    tags: Collection[str],
) -> None:
    """The background task: summarises what a trim of `file_id`'s body at
    `prompt_budget` characters would drop, if a new summary is due, and writes the
    result.

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
            document.body,
            document.context_summary,
            service=service,
            model=model,
            prompt_budget=prompt_budget,
            summary_num_ctx=summary_num_ctx,
            tags=tags,
        )
    except (LLMError, UnknownModel, RefusedProvider):
        logger.exception("the context-summary call failed for %s", file_id)
        return

    if outcome.section is None:
        return

    try:
        scanner.write_context_summary(file_id, document.body, outcome.section)
    except StorageError:
        logger.exception("could not write the context summary for %s", file_id)
