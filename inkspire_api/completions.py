# -*- coding: utf-8 -*-
"""Proposing one short sentence at the end of a chapter, note or one-shot.

    POST /api/stories/file/{id}/complete
    POST /api/notes/file/{id}/complete

Not a branch of `prompt.py`'s continuation/fill-in-the-middle/rewrite prompt, for the
same reason `titles.py` is not one: "write one sentence and stop" contradicts the
continuation branch's own instructions, and a fourth branch there would re-pin every
one of the 21 cases already recorded in `tests/data/prompts.json`. Its own template,
`templates/completion.j2`, keeps the continuation branch's join rules (never echo the
existing text, finish an interrupted word, begin with whatever joins on cleanly) and
replaces only the length instruction.

Answered in one response through `LLMService.complete`, not streamed -- one sentence
arrives essentially all at once, and streaming a proposal the writer has not asked to
see yet would be a stranger thing to show than the whole of it appearing at once.

Reads whichever model the writer selected for their prose, unlike `titles.py` and
`summaries.py`: the proposal is prose that may be accepted straight into the chapter,
so the model that answers it is the one chosen to write that chapter. The cost
`settings.py` names for a background call on the writer's own model -- it may be
hosted and metered -- is answered three ways: `llm_completion_tokens` caps how much a
single proposal may cost, `app.state.completion_limiter` is its own budget separate
from `/generate`'s, and the frontend's own toggle defaults off.

Both roots, unlike `titles.py`'s stories-only route: nothing here needs a story, and
the editor opens a note through the same component it opens a chapter in.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Collection

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from . import prompt as prompt_lib
from .deps import CurrentUser, SettingsDep
from .files import NotesDep, ScannerDep
from .llm import MAX_MODEL_LENGTH, LLMError, RefusedProvider, ServiceDep, UnknownModel

stories_router = APIRouter(prefix="/stories/file", tags=["completions"])
notes_router = APIRouter(prefix="/notes/file", tags=["completions"])

COMPLETION_TEMPLATE = "completion.j2"

#: A proposal this long has stopped being "one short sentence" and is a sign
#: `clean_completion`'s sentence cut did not find one -- kept as a hard backstop
#: rather than trusted to carry the whole answer.
MAX_COMPLETION_CHARS = 400

#: Where a sentence is taken to end, for cutting a capped answer back to its first
#: complete one. Each may be followed by a closing quote or bracket, which is kept
#: with it rather than left to open the next "sentence".
_SENTENCE_END = re.compile(r"[.!?…](?:[\"'”’)\]]*)")

EMPTY_COMPLETION_FILE = "Nothing to continue: the file is empty."
NOTHING_USABLE = "The model answered with nothing usable as a completion."


@dataclasses.dataclass(frozen=True)
class CompletionContext:
    """What `completion.j2` can render -- the text immediately before the caret,
    and nothing else.

    No `synopsis`, no dropped-context `summary`: a one-sentence join is driven by
    what the prose says right before it, and each added field is another pinned
    case for a proposal that is deliberately the cheapest, most frequent call this
    server makes. `PromptContext` grows a field per section as each is built
    (its own docstring); this one is not meant to grow the same way.
    """

    prefix: str


class CompletionRequest(BaseModel):
    """What a completion needs beyond the file itself: the model to ask, and
    nothing else. The settings-panel overrides `GenerateRequest` accepts are the
    writer's own choices about *their* generation; this one has a fixed shape."""

    model: str = Field(max_length=MAX_MODEL_LENGTH)


def assemble_completion(body: str, *, budget: int) -> CompletionContext:
    """`body`'s tail within `budget` characters, cut at a paragraph boundary.

    The tail, not the head `titles.py` and `summaries.py` keep: unlike a title or a
    dropped-context summary, what matters here is what the caret sits after.
    """
    return CompletionContext(prefix=prompt_lib.trim_to_tail(body, budget))


def render_completion(context: CompletionContext) -> str:
    """Wraps `context` in the completion instructions sent to the model."""
    return prompt_lib.JINJA.get_template(COMPLETION_TEMPLATE).render(ctx=context)


def clean_completion(raw: str) -> str:
    """`raw`, a model's answer, turned into something fit to insert at the caret.

    Two rules set this apart from `clean_title`'s cleanup:

    - **Leading whitespace is kept.** The template asks the model to begin with
      whatever joins on cleanly -- no character mid-word, a space after a finished
      word, a newline for a new line. Stripping the front would break that join.
    - **Cut back to the first complete sentence.** A model stopped by
      `llm_completion_tokens` usually stops mid-sentence, and a half-sentence ghost
      reads as a bug rather than a proposal. Cut at the first match of
      `_SENTENCE_END`, carrying a trailing quote or bracket with it; an answer with
      no sentence end at all is kept whole rather than discarded.

    Beyond that: a surrounding quote pair and a leading `Continuation:`-style label
    are stripped, trailing whitespace is dropped, and the result is capped at
    `MAX_COMPLETION_CHARS`. Answers "" for nothing usable, which the caller turns
    into a 502 rather than proposing the empty string.
    """
    if not raw.strip():
        return ""

    text = raw.rstrip()
    stripped = text.strip()
    for opening, closing in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        if len(stripped) >= 2 and stripped.startswith(opening) and stripped.endswith(closing):
            # The quotes wrap the whole answer: safe to drop both without
            # disturbing the leading whitespace rule, since none is left once
            # a surrounding quote is what opened the answer.
            text = stripped[1:-1]
            break

    lowered = text.lstrip().lower()
    for prefix in ("continuation:", "completion:", "proposal:"):
        if lowered.startswith(prefix):
            # `.strip()`, not the leading-whitespace-preserving rule above: the
            # space after a label like "Continuation: " is the label's own
            # formatting, not whitespace the model chose for the join.
            text = text.lstrip()[len(prefix) :].strip()
            break

    match = _SENTENCE_END.search(text)
    if match is not None:
        text = text[: match.end()]

    text = text.rstrip()
    return text[:MAX_COMPLETION_CHARS]


async def _complete(
    text: str,
    tags: Collection[str],
    body: CompletionRequest,
    *,
    request: Request,
    user: CurrentUser,
    service: ServiceDep,
    settings: SettingsDep,
) -> dict:
    """Shared body of both routes: the limiter, the empty-file check, the model
    call and the cleanup. `text` is the caller's own `read_document(...).body`, and
    `tags` the file's, which a provider may refuse."""
    if not request.app.state.completion_limiter.consume(user.email):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    if not text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, EMPTY_COMPLETION_FILE)

    prompt = render_completion(
        assemble_completion(text, budget=service.defaults.prompt_budget)
    )
    try:
        # A one-sentence proposal has no business reasoning out loud, and the
        # writer's own model may well be a reasoning one -- explicit here rather
        # than left to whatever INKSPIRE_LLM_THINK happens to be, the same reason
        # `titles.py` gives.
        raw = await service.complete(
            body.model,
            prompt,
            tags=tags,
            options=service.defaults.overlay(think=False),
            max_output_tokens=settings.llm_completion_tokens,
        )
    except UnknownModel as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except RefusedProvider as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    except LLMError as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from error

    completion = clean_completion(raw)
    if not completion:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, NOTHING_USABLE)
    return {"completion": completion}


@stories_router.post("/{file_id}/complete")
async def complete_story(
    file_id: str,
    body: CompletionRequest,
    scanner: ScannerDep,
    *,
    request: Request,
    user: CurrentUser,
    service: ServiceDep,
    settings: SettingsDep,
) -> dict:
    """Proposes one short sentence continuing this chapter or one-shot."""
    text = scanner.read_document(file_id).body
    return await _complete(
        text, scanner.file(file_id).tags, body, request=request, user=user, service=service, settings=settings
    )


@notes_router.post("/{file_id}/complete")
async def complete_note(
    file_id: str,
    body: CompletionRequest,
    notes: NotesDep,
    *,
    request: Request,
    user: CurrentUser,
    service: ServiceDep,
    settings: SettingsDep,
) -> dict:
    """Proposes one short sentence continuing this note."""
    text = notes.read_document(file_id).body
    return await _complete(
        text, notes.note(file_id).tags, body, request=request, user=user, service=service, settings=settings
    )
