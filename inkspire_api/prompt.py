# -*- coding: utf-8 -*-
"""What a generation asks a model, assembled server-side from the file on disk.

`PromptContext` is every section the template can render; only `text` -- the writer's
own prose -- is populated today. A story's synopsis, a chapter or character summary,
retrieved lore and the writer's own instruction are each a future field here plus a
guarded block in `templates/prompt.j2`, and nothing else changes: see `docs/prompt.md`
for the order they are meant to land in.

`trim_to_tail` is the one piece of policy in this module: a file longer than the budget
is cut to its tail at a paragraph boundary, using `provenance.split_paragraphs` -- the
same rule the provenance key is built on and the one the frontend matches byte for
byte. Writing a second paragraph rule here would disagree with the record a `.ink` file
already keeps.
"""

from __future__ import annotations

import dataclasses

import jinja2

from . import provenance

#: Autoescaping is off: the writer's apostrophes and quotes must reach the model as
#: typed, not as HTML entities.
JINJA = jinja2.Environment(
    loader=jinja2.PackageLoader("inkspire_api", "templates"),
    autoescape=False,
    keep_trailing_newline=False,
)

PROMPT_TEMPLATE = "prompt.j2"


@dataclasses.dataclass(frozen=True)
class PromptContext:
    """Every section the prompt template can render.

    `text` is the only field populated today -- the writer's prose, already trimmed to
    the budget. Each later section is a field added here, never a change to an existing
    one, so the template and its ten pinned cases (`tests/data/prompts.json`) are
    affected only by the section actually being rendered.
    """

    text: str


def _trim_paragraph_tail(paragraph: str, trailing_separator: str, budget: int) -> str:
    """The ladder for one paragraph that alone exceeds the budget.

    Whole lines from the tail first, falling back to a hard character cut if even the
    last line alone is too long -- always keeping the tail, per #12's chunk ladder.
    """
    text_budget = max(budget - len(trailing_separator), 0)
    lines = paragraph.split("\n")
    kept = [lines[-1]]
    total = len(lines[-1])
    for line in reversed(lines[:-1]):
        cost = len(line) + 1  # +1 for the newline that joins it back on
        if total + cost > text_budget:
            break
        kept.insert(0, line)
        total += cost

    if total <= text_budget:
        return "\n".join(kept) + trailing_separator

    # The single remaining line is itself too long: a hard cut, tail kept.
    tail = lines[-1][-text_budget:] if text_budget > 0 else ""
    return tail + trailing_separator


def trim_to_tail(body: str, budget: int) -> str:
    """`body`'s tail within `budget` characters, cut at a paragraph boundary.

    Under budget, `body` comes back byte-identical, trailing newline included. Over
    budget, whole paragraphs are kept from the end backwards; the leading separator of
    whatever is kept is dropped so the result never opens on a blank line, while the
    trailing one is kept so the trim changes nothing about how the body ends. A single
    paragraph that alone exceeds the budget falls to `_trim_paragraph_tail`'s ladder.
    """
    if len(body) <= budget:
        return body

    paragraphs, separators = provenance.split_paragraphs(body)
    if not paragraphs:
        # Nothing but separators (blank lines), and still over budget.
        return body[-budget:] if budget > 0 else ""

    last_cost = len(paragraphs[-1]) + len(separators[-1])
    if last_cost > budget:
        return _trim_paragraph_tail(paragraphs[-1], separators[-1], budget)

    start = len(paragraphs) - 1
    total = last_cost
    for i in range(len(paragraphs) - 2, -1, -1):
        cost = len(paragraphs[i]) + len(separators[i + 1])
        if total + cost > budget:
            break
        total += cost
        start = i

    kept_paragraphs = paragraphs[start:]
    kept_separators = [""] + separators[start + 1 :]
    return provenance.join_paragraphs(kept_paragraphs, kept_separators)


def assemble(body: str, *, budget: int) -> PromptContext:
    """The context for one generation: the writer's prose, trimmed to its tail within
    `budget` characters. Under budget, `body` enters the context unchanged."""
    return PromptContext(text=trim_to_tail(body, budget))


def render(context: PromptContext) -> str:
    """Wraps the context's sections in the continuation instructions sent to the model."""
    return JINJA.get_template(PROMPT_TEMPLATE).render(ctx=context)
