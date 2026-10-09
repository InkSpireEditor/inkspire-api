# -*- coding: utf-8 -*-
"""Proposing a chapter title.

    POST /api/stories/file/{id}/title

Not a branch of `prompt.py`'s continuation/fill-in-the-middle/rewrite prompt: a title
is a different question with its own instruction set, so it gets its own context and
its own template (`templates/title.j2`) rather than a fifth field on `PromptContext`
and a fourth branch in `prompt.j2` -- see that class's own docstring for why. Answered
in one response through `LLMService.complete`, not streamed: a title is a handful of
words, arriving essentially all at once.

Reads `INKSPIRE_LLM_SMALL_MODEL` rather than whichever model the writer picked for
their prose -- that model may be hosted and metered, chosen for writing rather than
for this. api#18's background summary call is meant to share this same setting
rather than add one of its own. Unset, and the route refuses with 409 rather than
falling back to the writer's model.

The request body is optional and, for the dice button, empty: `TitleRequest.instruction`
is the writer's own steering, typed into the title-edit modal rather than the dice itself.
"""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from . import prompt as prompt_lib
from .deps import CurrentUser, SettingsDep
from .files import ScannerDep
from .fs import MAX_NAME_LENGTH
from .llm import LLMError, ServiceDep, UnknownModel

router = APIRouter(prefix="/stories/file", tags=["titles"])

TITLE_TEMPLATE = "title.j2"

#: Ceiling on the writer's own steering, mirroring how `fs.MAX_NAME_LENGTH` is
#: already the shared ceiling for a name -- a short nudge, not a second prompt.
MAX_INSTRUCTION_LENGTH = 300

EMPTY_TITLE_FILE = "Nothing to title: the chapter is empty."
NO_SMALL_MODEL = (
    "No small model is configured. Set INKSPIRE_LLM_SMALL_MODEL to a model this "
    "feature may call."
)
NOTHING_USABLE = "The model answered with nothing usable as a title."


class TitleRequest(BaseModel):
    """What a title proposal may add to the server's own context.

    Nothing, by default: the server already knows the chapter's prose and its
    current title. `instruction` is the writer's own steering -- "something
    ominous," "shorter" -- used verbatim, for when the plain dice roll hasn't been
    satisfying.
    """

    instruction: str | None = Field(default=None, max_length=MAX_INSTRUCTION_LENGTH)


@dataclasses.dataclass(frozen=True)
class TitleContext:
    """What `title.j2` can render -- the chapter's own prose, the title it already
    has, if any, and the writer's own instruction, if they gave one.

    `current_title` is what lets a reroll ask for something other than a rewording
    of the first answer -- without it, the model has no way to know its last answer
    is already sitting there, and resampling it tends to repeat it almost verbatim.
    `instruction` is the writer's own steering, read from the title-edit modal's
    own field, not the dice button, which sends none. Both default to `""`, which
    renders no mention of either (the CLI, with no file or modal behind it, often
    has nothing to pass for either).

    Deliberately plain data -- no derived property joining the two into one
    "extra" string. That composition (whichever of the two paragraphs apply,
    with one blank line between them) lives in `title.j2` itself, as a `{% set
    %}` block each, so the actual wording of either paragraph stays something
    this file's own prompt, not Python source, decides -- see the template for
    why joining them needs more than a second independent `{% if %}` block.

    A story's synopsis is the natural next field here, the same way `PromptContext`
    grows one field per section as each is built; not added before anything needs it.
    """

    prose: str
    current_title: str = ""
    instruction: str = ""


def assemble_title(
    body: str, *, budget: int, current_title: str = "", instruction: str = ""
) -> TitleContext:
    """`body`'s opening within `budget` characters, cut at a paragraph boundary,
    alongside the chapter's `current_title` and the writer's own `instruction`.

    The head, not the tail `prompt_lib.trim_to_tail` keeps for a continuation's
    prefix: a chapter's opening is what establishes what it is about, and most
    chapters fit the budget whole regardless of which end would be kept.
    """
    return TitleContext(
        prose=prompt_lib.trim_to_head(body, budget),
        current_title=current_title,
        instruction=instruction.strip(),
    )


def render_title(context: TitleContext) -> str:
    """Wraps `context` in the title instructions sent to the model."""
    return prompt_lib.JINJA.get_template(TITLE_TEMPLATE).render(ctx=context)


def clean_title(raw: str) -> str:
    """`raw`, a small model's answer, turned into something fit to rename a chapter to.

    A model asked for a title reliably adds something that is not part of one: a
    surrounding quote, a "Title:" or "Chapter 3:" prefix, a trailing full stop, or
    several candidates on separate lines when only one was asked for. Each is
    stripped in turn; what remains is collapsed to single spaces and capped at the
    same length `files.py` enforces on any other name. Answers "" for nothing usable,
    which the caller turns into a 502 rather than renaming a chapter to the empty
    string.
    """
    first = next((line.strip() for line in raw.splitlines() if line.strip()), "")
    if not first:
        return ""

    for opening, closing in (('"', '"'), ("'", "'"), ("“", "”"), ("‘", "’")):
        if len(first) >= 2 and first.startswith(opening) and first.endswith(closing):
            first = first[1:-1].strip()
            break

    lowered = first.lower()
    for prefix in ("title:", "chapter title:", "proposed title:"):
        if lowered.startswith(prefix):
            first = first[len(prefix) :].strip()
            lowered = first.lower()
            break

    first = first.rstrip(".").strip()
    first = " ".join(first.split())
    return first[:MAX_NAME_LENGTH]


@router.post("/{file_id}/title")
async def propose_title(
    file_id: str,
    body: TitleRequest = TitleRequest(),
    *,
    request: Request,
    user: CurrentUser,
    scanner: ScannerDep,
    service: ServiceDep,
    settings: SettingsDep,
) -> dict:
    """Asks the configured small model for a short title for this chapter.

    Costs the same rate-limit token a generation does -- cheap and short, but still a
    model call, and `app.state.llm_limiter` is shared with `/generate` rather than
    given its own budget. Reads the file fresh, the same way a generation does.
    `body.instruction` is the writer's own steering, typed into the title-edit
    modal -- the dice button sends none, which is why `body` defaults to an empty
    `TitleRequest` rather than being required.
    """
    if not request.app.state.llm_limiter.consume(user.email):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    if settings.llm_small_model is None:
        raise HTTPException(status.HTTP_409_CONFLICT, NO_SMALL_MODEL)

    current_title = scanner.file(file_id).name
    text = scanner.read_document(file_id).body
    if not text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, EMPTY_TITLE_FILE)

    prompt = render_title(
        assemble_title(
            text,
            budget=service.defaults.prompt_budget,
            current_title=current_title,
            instruction=body.instruction or "",
        )
    )
    try:
        # A title has no business reasoning out loud, and a small model with a small
        # window can spend all of it thinking and answer nothing -- explicit here
        # rather than left to whatever INKSPIRE_LLM_THINK happens to be.
        raw = await service.complete(
            settings.llm_small_model, prompt, options=service.defaults.overlay(think=False)
        )
    except UnknownModel as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except LLMError as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from error

    title = clean_title(raw)
    if not title:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, NOTHING_USABLE)
    return {"title": title}
