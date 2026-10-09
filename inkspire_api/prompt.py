# -*- coding: utf-8 -*-
"""What a generation asks a model, assembled server-side from the file on disk.

`PromptContext` is every section the template can render: `prefix`, `suffix`,
`selection_words` and a story's synopsis or a notes folder's context (`synopsis`,
api#17). A chapter or character summary, retrieved lore and the writer's own
instruction are each a future field here plus a guarded block in
`templates/prompt.j2`, and nothing else changes: see `docs/prompt.md` for the order
they are meant to land in.

A caret splits the body into a prefix and a suffix; with no caret the whole body is
the prefix and the suffix is empty, which is a continuation -- today's only mode and
still the common one. A selection -- a range rather than a point -- splits the body
into a prefix, a suffix and the selection itself; the selection's own text reaches
the prompt only when `assemble`'s `send_selection` says so -- its word count always
does (`docs/prompt.md` has why) -- which is what turns a rewrite into a third mode
rather than a wider fill-in-the-middle.
`trim_to_tail`/`trim_to_head` are the policy: a side longer than its share of the
budget is cut at a paragraph boundary, using `provenance.split_paragraphs` -- the same
rule the provenance key is built on and the one the frontend matches byte for byte.
Writing a second paragraph rule here would disagree with the record a `.ink` file
already keeps.
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


class InvertedRange(CursorOutOfRange):
    """A selection's end comes before its start."""


@dataclasses.dataclass(frozen=True)
class PromptContext:
    """Every section the prompt template can render.

    `prefix` and `suffix` are the writer's prose either side of the caret or the
    selection, already trimmed to their share of the budget. `selection_words` is
    `None` for a continuation or a fill-in-the-middle, and the selected passage's own
    word count for a rewrite. `selection` is the passage's own text when `assemble`
    was asked to send it (`send_selection=True`), and `None` otherwise -- including a
    rewrite whose caller declined to send it, which still has `selection_words` set
    (`docs/prompt.md` has why both exist). An empty `suffix` with no `selection_words`
    is a continuation (no caret, or a caret at the end of the file); the template
    renders each of the three differently rather than one instruction set for all
    (`docs/prompt.md`). `synopsis` (api#17) is a story's own synopsis or a notes
    folder's own context -- both the same `Folder.summary` in memory, whichever
    applies to the file being generated from, or `""` for a one-shot or a root-level
    note, neither of which has one. Unlike the other fields, it is never trimmed or
    charged against the budget: short by construction at the source. Each later
    section is a field added here, never a change to an existing one, so the
    template and its pinned cases (`tests/data/prompts.json`) are affected only by
    the section actually being rendered.
    """

    prefix: str
    suffix: str = ""
    selection_words: int | None = None
    selection: str | None = None
    synopsis: str = ""


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


@dataclasses.dataclass(frozen=True)
class CursorRange:
    """A selection's anchor: where it starts and where it ends.

    `start == end` is a collapsed range -- a caret -- and `assemble` treats it exactly
    as `cursor=` would; `split_at_range` is only ever reached for a real range. Built
    from two `Cursor`s so the paragraph-and-offset addressing a single caret already
    uses is reused rather than invented a second time.
    """

    start: Cursor
    end: Cursor


#: One side's paragraphs and the separators around them -- the shape `split_paragraphs`,
#: `split_at_cursor` and `split_at_range` all return one piece of.
Side = tuple[list[str], list[str]]


def count_words(text: str) -> int:
    """`text`'s word count, split on whitespace.

    Wrong for a script with no spaces between words -- CJK, chiefly -- the same way
    `INKSPIRE_LLM_PROMPT_BUDGET`'s characters-per-token assumption already is
    (`docs/prompt.md`). Not corrected for here.
    """
    return len(text.split())


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


def split_at_range(
    paragraphs: list[str], separators: list[str], anchor: CursorRange
) -> tuple[Side, Side, Side]:
    """`paragraphs`/`separators` split at `anchor` into a prefix, the selection itself
    and a suffix, each as a `(paragraphs, separators)` pair.

    The prefix and the suffix are built by calling `split_at_cursor` at `anchor.start`
    and `anchor.end` respectively and keeping only the half each call needs -- so both
    ends are validated by the one function that already validates one, rather than a
    second copy of the same range checks. Only the middle -- the selection -- is new:
    the paragraph(s) between the two cursors, cut at each one's offset the same way
    `split_at_cursor` cuts at one.

    Raises `InvertedRange` if `anchor.end` comes before `anchor.start`, and
    `CursorOutOfRange` (via `split_at_cursor`) if either end does not address a
    character of `paragraphs`.
    """
    start, end = anchor.start, anchor.end
    if (end.para, end.offset) < (start.para, start.offset):
        raise InvertedRange("the selection's end comes before its start.")

    prefix_paragraphs, prefix_separators, _, _ = split_at_cursor(paragraphs, separators, start)
    _, _, suffix_paragraphs, suffix_separators = split_at_cursor(paragraphs, separators, end)

    if start.para == end.para:
        selection_paragraphs = [paragraphs[start.para][start.offset : end.offset]]
        selection_separators = ["", ""]
    else:
        selection_paragraphs = (
            [paragraphs[start.para][start.offset :]]
            + paragraphs[start.para + 1 : end.para]
            + [paragraphs[end.para][: end.offset]]
        )
        selection_separators = [""] + separators[start.para + 1 : end.para + 1] + [""]

    return (
        (prefix_paragraphs, prefix_separators),
        (selection_paragraphs, selection_separators),
        (suffix_paragraphs, suffix_separators),
    )


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


def _ends_body(paragraphs: list[str], cursor: Cursor) -> bool:
    """Whether `cursor` sits at the very end of the last paragraph -- nothing after it
    but the body's own closing separator."""
    return cursor.para == len(paragraphs) - 1 and cursor.offset == len(paragraphs[cursor.para])


def _resolve_sides(
    paragraphs: list[str], separators: list[str], anchor: CursorRange | None
) -> tuple[Side, Side, str | None]:
    """The prefix, suffix and selected passage for `anchor`, as
    `(prefix, suffix, passage)`.

    `passage` is the selected text itself, not its word count -- `assemble` counts it
    and decides separately whether to forward the text or just the count
    (`send_selection`, `docs/prompt.md`).

    No anchor, or one collapsed to a single point sitting at the very end of the last
    paragraph, gives the whole body as the prefix, an empty suffix and no passage --
    a continuation. The end-of-file case has to be detected here rather than left to
    `split_at_cursor`/`split_at_range`: physically, the body's own closing separator
    sits after the cursor, so a literal split would hand it to the suffix and the
    template would send fill-in-the-middle or rewrite instructions for a file that
    merely ends with a blank line. The split still runs in that case, so an otherwise
    invalid cursor there still raises -- its result is only discarded.

    A selection of nothing but whitespace (`count_words` of its own text is `0`)
    degenerates to a collapsed anchor at its own start, so a rewrite can never be
    asked for zero words -- it becomes the caret case instead.
    """
    if anchor is None:
        return (paragraphs, separators), ([], [""]), None
    if not paragraphs:
        raise CursorOutOfRange("the body is empty; there is no paragraph to address.")

    if anchor.start == anchor.end:
        cursor = anchor.start
        split = split_at_cursor(paragraphs, separators, cursor)
        if _ends_body(paragraphs, cursor):
            return (paragraphs, separators), ([], [""]), None
        return (split[0], split[1]), (split[2], split[3]), None

    prefix_side, selection_side, suffix_side = split_at_range(paragraphs, separators, anchor)
    passage = provenance.join_paragraphs(*selection_side)
    if count_words(passage) == 0:
        return _resolve_sides(paragraphs, separators, CursorRange(anchor.start, anchor.start))

    if _ends_body(paragraphs, anchor.end):
        suffix_side = ([], [""])
    return prefix_side, suffix_side, passage


def _anchor(cursor: Cursor | None, selection: CursorRange | None) -> CursorRange | None:
    """`cursor` and `selection`, normalised to the one shape `_resolve_sides` takes.

    Raises `ValueError` if both are given -- a programming error, not something a
    request can cause: `llm.py`'s `_anchor_from_request` never produces both.
    """
    if cursor is not None and selection is not None:
        raise ValueError("assemble() takes a cursor or a selection, not both.")
    if selection is not None:
        return selection
    if cursor is not None:
        return CursorRange(cursor, cursor)
    return None


def _prefix_budget(budget: int, prefix_share: float, suffix_body: str) -> int:
    """How many of `budget` characters go to the prefix.

    The whole budget, when there is no suffix to apportion it to -- the common case,
    and the one that must not lose a quarter of its budget to a suffix that is empty
    anyway. Otherwise `prefix_share`'s share of it, floored; the suffix gets whatever
    is left.
    """
    if not suffix_body:
        return budget
    return math.floor(budget * prefix_share)


def _prose_budget(budget: int, shown: str | None) -> int:
    """`budget`, less a passage being shown to the model -- `budget` unchanged when
    none is, or none is being sent.

    A shown passage is never itself trimmed (`docs/prompt.md` has why: a partial
    passage with a request to replace all of it is worse than no passage at all), so
    it is charged against the budget before the prefix and the suffix split whatever
    is left, floored at `0` rather than going negative when the passage alone exceeds
    `budget` -- in which case both sides end up empty.
    """
    if not shown:
        return budget
    return max(budget - len(shown), 0)


def _trim_sides(
    prefix_side: Side, suffix_side: Side, budget: int, prefix_share: float
) -> tuple[str, str]:
    """The prefix and the suffix, each already joined and trimmed to its share of
    `budget` characters, as `(prefix, suffix)`."""
    suffix_body = provenance.join_paragraphs(*suffix_side)
    prefix_budget = _prefix_budget(budget, prefix_share, suffix_body)
    trimmed_prefix = trim_to_tail(provenance.join_paragraphs(*prefix_side), prefix_budget)
    trimmed_suffix = trim_to_head(suffix_body, budget - prefix_budget) if suffix_body else ""
    return trimmed_prefix, trimmed_suffix


def assemble(
    body: str,
    *,
    budget: int,
    cursor: Cursor | None = None,
    selection: CursorRange | None = None,
    prefix_share: float = 0.75,
    send_selection: bool = False,
    synopsis: str = "",
) -> PromptContext:
    """The context for one generation: `body` split at `cursor` or `selection`, each
    side trimmed to its share of `budget` characters.

    With neither `cursor` nor `selection` given, the whole body is the prefix and the
    suffix is empty -- a continuation, byte-identical to assembling with no caret at
    all existed. The budget is apportioned only once there is a suffix to apportion
    it to: a cursor or a selection ending at the very end of the file degenerates to
    the same case, so the common path never loses a quarter of its budget to a suffix
    that is empty anyway.

    `send_selection` decides whether a real selection's own text reaches the prompt as
    `PromptContext.selection`, or only its word count does, on `selection_words`
    (`docs/prompt.md` has why both exist and why this defaults to off here while the
    server's own default is on -- `llm.py`'s `GenerationOptions`). When it is sent, it
    is charged against `budget` before the prefix and the suffix split what remains,
    and it is never itself trimmed: a passage large enough to exhaust the budget
    leaves both sides empty rather than losing part of the text being replaced.

    `synopsis` (api#17) passes straight through to `PromptContext.synopsis` --
    unlike the selection, never charged against `budget` and never trimmed, since
    it is short by construction at the source (the frontend's own
    `MAX_SUMMARY_LENGTH`).

    Raises `ValueError` if both `cursor` and `selection` are given, and
    `CursorOutOfRange` (or its `InvertedRange` subclass) if either does not address
    `body`.
    """
    anchor = _anchor(cursor, selection)
    paragraphs, separators = provenance.split_paragraphs(body)
    prefix_side, suffix_side, passage = _resolve_sides(paragraphs, separators, anchor)
    shown = passage if send_selection else None

    trimmed_prefix, trimmed_suffix = _trim_sides(
        prefix_side, suffix_side, _prose_budget(budget, shown), prefix_share
    )
    return PromptContext(
        prefix=trimmed_prefix,
        suffix=trimmed_suffix,
        selection_words=count_words(passage) if passage is not None else None,
        selection=shown,
        synopsis=synopsis,
    )


def render(context: PromptContext) -> str:
    """Wraps the context's sections in the rewrite, fill-in-the-middle or
    continuation instructions sent to the model, chosen by `context.selection_words`
    and then by whether `context.suffix` is empty. Within the rewrite branch,
    `context.selection` present or absent chooses between showing the passage and the
    bare `[REPLACE THIS]` marker."""
    return JINJA.get_template(PROMPT_TEMPLATE).render(ctx=context)
