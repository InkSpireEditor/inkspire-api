# -*- coding: utf-8 -*-
"""The `ink:context_summary` section: its format, and the trigger metric (api#18).

Split out of `summaries.py` so that `llm.py` can read a stored section without a cyclic
import: `summaries.py` calls a small model and therefore imports `LLMService` from
`llm.py`, while `llm.py`'s own generate routes need to parse what is already stored --
importing `summaries.py` from `llm.py` would close that loop. Nothing here imports
`llm.py`, the same way `provenance.py` carries the sibling `ink:provenance` section's
format with no dependency on anything that calls a model.

See `summaries.py` for why this section exists at all, and `prompt.py`'s
`PromptContext.summary` for why it renders only when a request's own trim actually cuts
the prefix.
"""

from __future__ import annotations

import dataclasses
import logging

import yaml

from . import prompt as prompt_lib
from .fs import dump_yaml

logger = logging.getLogger(__name__)

#: The smallest change in what is dropped that justifies a new call on its own, before
#: proportional growth applies -- a floor, so a chapter barely over budget does not get
#: a call for a couple of sentences' worth of change.
MIN_TRIGGER_CHARS = 2000

#: How much the dropped character count has to grow (or shrink) before a new call is
#: due, as a multiple of what is already stored -- 2.0 means the threshold doubles each
#: time (triggers at roughly 2k, 4k, 8k, 16k dropped characters: fewer calls, more
#: staleness between them). The gentler golden-ratio-ish 1.618 would trigger more often
#: (2k, 3.2k, 5.2k, 8.5k) at less staleness each time. Start with doubling.
GROWTH_FACTOR = 2.0


@dataclasses.dataclass(frozen=True)
class ContextSummary:
    """One file's stored summary: the text, and how many characters were dropped
    when it was computed.

    `dropped_chars` is what `is_due` compares a later trim's own count against to
    decide whether the summary is stale enough to recompute.
    """

    text: str
    dropped_chars: int


def render_section(summary: ContextSummary) -> str:
    """`summary` as the text of an `ink:context_summary` section.

    `dump_yaml` already writes a multi-line string as a `|` block, which is what
    keeps the summary itself readable by a person who opens the file.
    """
    return dump_yaml({"dropped_chars": summary.dropped_chars, "summary": summary.text})


def parse_section(text: str) -> ContextSummary | None:
    """An `ink:context_summary` section's text as a `ContextSummary`, or `None` where
    it cannot be read at all -- not YAML, not a mapping, or missing either key.

    `None` is read the same way an unreadable `ink:provenance` section is
    (`provenance.parse_section`): as "nothing stored," which is recoverable -- the
    next due summary call simply writes a fresh one -- rather than a guess at a
    section that might mean anything.
    """
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as error:
        logger.warning("the context-summary section could not be parsed: %s", error)
        return None
    if not isinstance(loaded, dict):
        return None
    summary, dropped_chars = loaded.get("summary"), loaded.get("dropped_chars")
    if not isinstance(summary, str) or not isinstance(dropped_chars, int):
        return None
    return ContextSummary(text=summary, dropped_chars=dropped_chars)


def dropped(body: str, budget: int) -> str:
    """What a continuation's `trim_to_tail` would cut from `body`'s prefix at
    `budget` characters -- `""` when nothing is cut.

    Correct for every branch `trim_to_tail` takes, including the single-paragraph
    ladder and a body of nothing but separators, because what it keeps is always a
    literal suffix of `body`: `_keep_from_tail` drops the leading separator of
    whatever it keeps, so that separator -- like everything before it -- lands on
    the dropped side here.
    """
    kept = prompt_lib.trim_to_tail(body, budget)
    return body[: len(body) - len(kept)]


def is_due(dropped_chars: int, stored: int) -> bool:
    """Whether a new summary call is justified, given what is dropped now and what
    the last stored summary was computed against.

    Proportional rather than a fixed size: the longer a story, the less any one new
    paragraph should matter, so the amount of change needed to justify a new call
    has to grow with how much is already dropped. `MIN_TRIGGER_CHARS` is a floor
    under that growth, so a chapter barely over budget does not get a call for a
    couple of sentences' worth of change.

    The `- 1` is not optional: it turns "the total must double" into "the *change*
    must equal one multiple of what is already stored," which is what lets the same
    check run symmetrically in both directions through `abs()`. Dropping it would
    make the growing case require tripling instead of doubling.

    **Symmetric on purpose.** A growth-only check never notices a deletion:
    `dropped_chars` falling never crosses an upward-only threshold again, so a
    summary describing paragraphs the writer has since cut could stand forever.
    `abs()` catches a large deletion the same way it catches large growth, at the
    same proportional significance -- `summaries.update` is what then decides
    whether to write anything further once a deletion also means nothing is dropped
    at all.
    """
    delta = abs(dropped_chars - stored)
    return delta >= max(MIN_TRIGGER_CHARS, stored * (GROWTH_FACTOR - 1))
