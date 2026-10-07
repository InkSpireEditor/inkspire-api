# -*- coding: utf-8 -*-
"""What a generation asks a model, assembled server-side from the file on disk.

`PromptContext` is every section the template can render: `prefix` and `suffix`, the
text either side of the caret, and nothing else today. A story's synopsis, a chapter
or character summary, retrieved lore and the writer's own instruction are each a
future field here plus a guarded block in `templates/prompt.j2`, and nothing else
changes: see `docs/prompt.md` for the order they are meant to land in.

A caret splits the body into a prefix and a suffix; with no caret the whole body is
the prefix and the suffix is empty, which is a continuation -- today's only mode and
still the common one. `trim_to_tail`/`trim_to_head` are the policy: a side longer than
its share of the budget is cut at a paragraph boundary, using
`provenance.split_paragraphs` -- the same rule the provenance key is built on and the
one the frontend matches byte for byte. Writing a second paragraph rule here would
disagree with the record a `.ink` file already keeps.
"""

from __future__ import annotations

import dataclasses
import math

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


class CursorOutOfRange(ValueError):
    """A caret does not address the body it is reported against."""


@dataclasses.dataclass(frozen=True)
class PromptContext:
    """Every section the prompt template can render.

    `prefix` and `suffix` are the only fields populated today -- the writer's prose
    either side of the caret, already trimmed to their share of the budget. An empty
    `suffix` is a continuation (no caret, or a caret at the end of the file); the
    template renders the two differently rather than one instruction set for both
    (`docs/prompt.md`). Each later section is a field added here, never a change to an
    existing one, so the template and its pinned cases (`tests/data/prompts.json`) are
    affected only by the section actually being rendered.
    """

    prefix: str
    suffix: str = ""


@dataclasses.dataclass(frozen=True)
class Cursor:
    """A caret's position: the paragraph it is in, and the offset within it.

    `offset` may equal the length of `paragraphs[para]` -- the caret sitting at the
    very end of that paragraph, before whatever separator follows it. That is how a
    caret on a blank line between two paragraphs is addressed: as the end of the one
    before it.
    """

    para: int
    offset: int


def _trim_paragraph_tail(paragraph: str, trailing_separator: str, budget: int) -> str:
    """The ladder for one paragraph that alone exceeds the budget, trimmed from the end.

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


def _trim_paragraph_head(paragraph: str, leading_separator: str, budget: int) -> str:
    """The mirror of `_trim_paragraph_tail`, trimmed from the start.

    Whole lines from the head first, falling back to a hard character cut if even the
    first line alone is too long -- always keeping the head.
    """
    text_budget = max(budget - len(leading_separator), 0)
    lines = paragraph.split("\n")
    kept = [lines[0]]
    total = len(lines[0])
    for line in lines[1:]:
        cost = len(line) + 1  # +1 for the newline that joined it on
        if total + cost > text_budget:
            break
        kept.append(line)
        total += cost

    if total <= text_budget:
        return leading_separator + "\n".join(kept)

    # The single remaining line is itself too long: a hard cut, head kept.
    head = lines[0][:text_budget] if text_budget > 0 else ""
    return leading_separator + head


def _keep_from_tail(
    paragraphs: list[str], separators: list[str], budget: int
) -> tuple[list[str], list[str]]:
    """Whole paragraphs kept from the end backwards, within `budget` characters.

    The leading separator of whatever is kept is dropped, so the result never opens on
    a blank line; the trailing one is kept as given, so a caller addressing the real
    end of a body keeps its exact ending, while one addressing a caret (whose trailing
    separator is already `""`) gets a clean cut.

    Assumes at least one paragraph. A single paragraph alone over budget falls to
    `_trim_paragraph_tail`'s ladder, returned already joined with its separator --
    the one case this returns a one-paragraph, already-separator-joined result instead
    of raw lists, since the ladder may cut inside the paragraph's own text.
    """
    last_cost = len(paragraphs[-1]) + len(separators[-1])
    if last_cost > budget:
        return [_trim_paragraph_tail(paragraphs[-1], separators[-1], budget)], ["", ""]

    start = len(paragraphs) - 1
    total = last_cost
    for i in range(len(paragraphs) - 2, -1, -1):
        cost = len(paragraphs[i]) + len(separators[i + 1])
        if total + cost > budget:
            break
        total += cost
        start = i

    return paragraphs[start:], [""] + separators[start + 1 :]


def _keep_from_head(
    paragraphs: list[str], separators: list[str], budget: int
) -> tuple[list[str], list[str]]:
    """The mirror of `_keep_from_tail`: whole paragraphs kept from the start forwards.

    The trailing separator of whatever is kept is dropped, so the result never ends on
    a blank line; the leading one is kept as given.

    Assumes at least one paragraph, for the same reason `_keep_from_tail` does.
    """
    first_cost = len(separators[0]) + len(paragraphs[0])
    if first_cost > budget:
        return [_trim_paragraph_head(paragraphs[0], separators[0], budget)], ["", ""]

    end = 1
    total = first_cost
    for i in range(1, len(paragraphs)):
        cost = len(separators[i]) + len(paragraphs[i])
        if total + cost > budget:
            break
        total += cost
        end = i + 1

    return paragraphs[:end], separators[:end] + [""]


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

    kept_paragraphs, kept_separators = _keep_from_tail(paragraphs, separators, budget)
    return provenance.join_paragraphs(kept_paragraphs, kept_separators)


def trim_to_head(body: str, budget: int) -> str:
    """The mirror of `trim_to_tail`: `body`'s head within `budget` characters.

    Over budget, whole paragraphs are kept from the start forwards, never ending on a
    blank line. Used for the suffix -- the text after a caret -- where `trim_to_tail`
    is used for the prefix.
    """
    if len(body) <= budget:
        return body

    paragraphs, separators = provenance.split_paragraphs(body)
    if not paragraphs:
        return body[:budget] if budget > 0 else ""

    kept_paragraphs, kept_separators = _keep_from_head(paragraphs, separators, budget)
    return provenance.join_paragraphs(kept_paragraphs, kept_separators)


def split_at_cursor(
    paragraphs: list[str], separators: list[str], cursor: Cursor
) -> tuple[list[str], list[str], list[str], list[str]]:
    """`paragraphs`/`separators` split at `cursor` into a prefix and a suffix.

    The paragraph the caret sits in is itself split at `cursor.offset`: its head joins
    the prefix as that paragraph's replacement, its tail joins the suffix the same way.
    The prefix's new trailing separator and the suffix's new leading separator are both
    `""`, because both now end or begin exactly at the caret -- not at whatever
    separator used to be there.

    Raises `CursorOutOfRange` if `cursor` does not address a character of `paragraphs`.
    """
    if not 0 <= cursor.para < len(paragraphs):
        raise CursorOutOfRange(
            f"paragraph {cursor.para} does not exist; the body has {len(paragraphs)}."
        )
    target = paragraphs[cursor.para]
    if not 0 <= cursor.offset <= len(target):
        raise CursorOutOfRange(
            f"offset {cursor.offset} is out of range for a paragraph of "
            f"{len(target)} characters."
        )

    c = cursor.para
    head, tail = target[: cursor.offset], target[cursor.offset :]

    prefix_paragraphs = paragraphs[:c] + [head]
    prefix_separators = separators[: c + 1] + [""]
    suffix_paragraphs = [tail] + paragraphs[c + 1 :]
    suffix_separators = [""] + separators[c + 1 :]
    return prefix_paragraphs, prefix_separators, suffix_paragraphs, suffix_separators


def cursor_from_offset(body: str, offset: int) -> Cursor:
    """The paragraph and in-paragraph offset a flat character `offset` lands at.

    An offset landing inside a separator resolves to the end of the paragraph before
    it -- or to the very start of the body, `Cursor(0, 0)`, if it is before the first
    paragraph. One past the body's own end resolves to the end of the last paragraph.
    Used by the CLI, which has only a flat offset into stdin to work from; the HTTP
    route is handed a paragraph and an offset directly.
    """
    paragraphs, separators = provenance.split_paragraphs(body)
    if not paragraphs:
        return Cursor(para=0, offset=0)

    offset = max(0, min(offset, len(body)))
    position = 0
    for i, paragraph in enumerate(paragraphs):
        start = position + len(separators[i])
        end = start + len(paragraph)
        if offset < start:
            if i == 0:
                return Cursor(para=0, offset=0)
            return Cursor(para=i - 1, offset=len(paragraphs[i - 1]))
        if offset <= end:
            return Cursor(para=i, offset=offset - start)
        position = end

    return Cursor(para=len(paragraphs) - 1, offset=len(paragraphs[-1]))


#: One side's paragraphs and the separators around them -- the shape `split_paragraphs`
#: and `split_at_cursor` both return one half of.
Side = tuple[list[str], list[str]]


def _resolve_sides(
    paragraphs: list[str], separators: list[str], cursor: Cursor | None
) -> tuple[Side, Side]:
    """The prefix and suffix sides for `cursor`, as `(paragraphs, separators)` pairs.

    No cursor, or one at the very end of the last paragraph, gives the whole body as
    the prefix and an empty suffix -- a continuation. The end-of-file case has to be
    detected here rather than left to `split_at_cursor`: physically, the body's own
    closing separator sits after the cursor, so a literal split would hand it to the
    suffix and the template would send fill-in-the-middle instructions for a file
    that merely ends with a blank line. `split_at_cursor` still runs in that case, so
    an otherwise-invalid cursor there still raises -- its result is only discarded.
    """
    if cursor is None:
        return (paragraphs, separators), ([], [""])
    if not paragraphs:
        raise CursorOutOfRange("the body is empty; there is no paragraph to address.")

    split = split_at_cursor(paragraphs, separators, cursor)
    at_the_end = cursor.para == len(paragraphs) - 1 and cursor.offset == len(
        paragraphs[cursor.para]
    )
    if at_the_end:
        return (paragraphs, separators), ([], [""])
    return (split[0], split[1]), (split[2], split[3])


def assemble(
    body: str,
    *,
    budget: int,
    cursor: Cursor | None = None,
    prefix_share: float = 0.75,
) -> PromptContext:
    """The context for one generation: `body` split at `cursor`, each side trimmed to
    its share of `budget` characters.

    With no `cursor`, the whole body is the prefix and the suffix is empty -- a
    continuation, byte-identical to assembling with no caret at all existed. The
    budget is apportioned only once there is a suffix to apportion it to: a caret at
    the very end of the file degenerates to the same case, so the common path never
    loses a quarter of its budget to a suffix that is empty anyway.

    Raises `CursorOutOfRange` if `cursor` does not address a character of `body`.
    """
    paragraphs, separators = provenance.split_paragraphs(body)
    prefix_side, suffix_side = _resolve_sides(paragraphs, separators, cursor)
    suffix_body = provenance.join_paragraphs(*suffix_side)

    if not suffix_body:
        prefix_budget, suffix_budget = budget, 0
    else:
        prefix_budget = math.floor(budget * prefix_share)
        suffix_budget = budget - prefix_budget

    trimmed_prefix = trim_to_tail(provenance.join_paragraphs(*prefix_side), prefix_budget)
    trimmed_suffix = trim_to_head(suffix_body, suffix_budget) if suffix_body else ""
    return PromptContext(prefix=trimmed_prefix, suffix=trimmed_suffix)


def render(context: PromptContext) -> str:
    """Wraps the context's sections in the continuation or fill-in-the-middle
    instructions sent to the model, chosen by whether `context.suffix` is empty."""
    return JINJA.get_template(PROMPT_TEMPLATE).render(ctx=context)
