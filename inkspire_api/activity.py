# -*- coding: utf-8 -*-
"""The words written per day, from the story repository's history.

    GET /api/stories/activity?days=365

Answers a list of `{"date", "words"}`, oldest first, one entry per day that has
any. The words of a day are the words its commits **added** to chapters and
one-shots: the words in the new body that the old one lacked, not the net gain.
A rewrite of one hundred words counts one hundred even when the passage ends up
the same length, and a deletion counts nothing.

What counts as prose is decided by `ink.parse(text).body`, the same split the
editor is served: the prose alone, never the `ink:meta`, `ink:provenance` or
`ink:context_summary` sections. `repository.history_records` hands this module the
whole files; `body_of` takes the prose out of each before anything is counted. A
save rewrites the provenance, so a commit that only changed a provenance record
or a header adds nothing.

**Only committed work counts.** A chapter typed today and not yet committed has
no day, and the notes root is not a repository, so notes are never counted.

The diff is taken paragraph-first. Comparing whole paragraphs with a
`SequenceMatcher` is cheap, and it keeps the word-level diff to the paragraphs
that actually changed; a word-level diff of two whole chapters is quadratic.
Inside a paragraph that changed, the diff runs again on words, where a word is a
run of non-whitespace — the same rule the frontend's `countWords` uses. A
replacement large enough to make that second diff quadratic is counted in full
instead: past `MAX_DIFF_WORDS`, every new word of the run counts, since a
passage replaced that heavily is new prose whatever part of it a diff would have
matched.

The day of a commit is its **author date as written, in its own offset**: a
commit authored at `2026-03-01T23:30:00+13:00` is on `2026-03-01`, not
converted to UTC or to the server's local date. The answer is held against
`HEAD` and today, and rebuilt only when either moves.
"""

from __future__ import annotations

import difflib
import threading
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from . import ink
from .provenance import split_paragraphs
from .repository import RepoDep, history_records

#: Past this many words in one replaced run, the run is counted in full rather
#: than word-diffed: a word-level diff is quadratic in the run's length, and a
#: passage replaced this heavily is new prose whatever part of it the diff
#: would have matched.
MAX_DIFF_WORDS = 3000

#: The longest window the cache computes. The frontend asks for 364 (52 weeks);
#: the route accepts up to 366 so a leap year is covered by the one window too.
WINDOW_DAYS = 366

router = APIRouter(prefix="/stories", tags=["activity"])


def body_of(text: str) -> str:
    """The prose of one `.ink` file: its `ink:body` section, or the whole text where
    the file has no sections. `""` stays `""`, which is a file that is not there."""
    return ink.parse(text).body


def words_added(old: str, new: str) -> int:
    """The words in the new body that the old one lacked.

    `old` and `new` are bodies (`body_of`), not whole files. Both are split into
    paragraphs with `provenance.split_paragraphs`, the one definition of where a
    paragraph ends. Paragraphs are compared first, and only a replacement's runs are
    diffed word by word. A word is a run of non-whitespace. `old` of `""` is a file
    that did not exist, or one without a parent commit; `new` of `""` is one the
    commit deleted.
    """
    old_paragraphs = split_paragraphs(old)[0]
    new_paragraphs = split_paragraphs(new)[0]

    matcher = difflib.SequenceMatcher(None, old_paragraphs, new_paragraphs, autojunk=False)
    words = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            words += _words(new_paragraphs[j1:j2])
        elif tag == "replace":
            old_run = " ".join(old_paragraphs[i1:i2]).split()
            new_run = " ".join(new_paragraphs[j1:j2]).split()
            if len(old_run) > MAX_DIFF_WORDS or len(new_run) > MAX_DIFF_WORDS:
                words += len(new_run)
                continue
            run_matcher = difflib.SequenceMatcher(None, old_run, new_run, autojunk=False)
            for run_tag, _, _, k1, k2 in run_matcher.get_opcodes():
                if run_tag in ("insert", "replace"):
                    words += k2 - k1
    return words


def _words(paragraphs: list[str]) -> int:
    """The words in paragraphs, a word being a run of non-whitespace."""
    return len(" ".join(paragraphs).split())


def by_day(
    records: list[tuple[date, int]], today: date, days: int
) -> list[dict]:
    """The words of each commit grouped into days, cut to the window, oldest first.

    `records` is one `(author date, words)` pair per commit. A day outside the
    last `days` calendar days ending today, inclusive, is not in the answer even
    if its commit was read; a day inside it with no words is absent rather than
    written as a zero.
    """
    totals: dict[date, int] = {}
    for day, words in records:
        totals[day] = totals.get(day, 0) + words

    start = today - timedelta(days=days - 1)
    return [
        {"date": day.isoformat(), "words": words}
        for day, words in sorted(totals.items())
        if start <= day <= today and words > 0
    ]


class ActivityCache:
    """The activity of one repository, computed for the longest window once.

    Reading a year of history and diffing it is heavy, and every request asks
    for a slice of the same window, so the whole window is computed once and
    sliced per request. Held against `(HEAD's commit, today)`: a new commit or
    a change of day rebuilds it. Sync endpoints run in a thread pool, so two
    requests can reach this at once and the lock covers the check and the
    recompute as one step.
    """

    def __init__(self, compute) -> None:
        self._compute = compute
        self._lock = threading.Lock()
        self._key: tuple | None = None
        self._records: list[tuple[date, int]] = []

    def for_window(self, repo, today: date, days: int) -> list[dict]:
        """The days in the window, recomputed first if `HEAD` or the day moved.

        A repository with no commits has no `HEAD` to key on, and answers
        nothing rather than raising.
        """
        try:
            sha = repo.head.commit.hexsha
        except ValueError:
            return []
        key = (sha, today)
        with self._lock:
            if key != self._key:
                self._records = self._compute(repo, today)
                self._key = key
            records = self._records
        return by_day(records, today, days)


def _compute(repo, today: date) -> list[tuple[date, int]]:
    """The words each day gained, summed per author-date day, oldest day first.

    The window is cut here rather than per request, so the cache slices a list
    of the days that can ever be asked for. Each commit's texts are counted as the
    history yields them and then dropped, so only one commit's files are held at a
    time.
    """
    start = today - timedelta(days=WINDOW_DAYS - 1)
    totals: dict[date, int] = {}
    for day, changes in history_records(repo, start):
        words = sum(words_added(body_of(old), body_of(new)) for old, new in changes)
        totals[day] = totals.get(day, 0) + words
    return [(day, words) for day, words in sorted(totals.items()) if words > 0]


def get_activity(request: Request) -> ActivityCache:
    """One cache per application, against the story repository's `HEAD`."""
    if request.app.state.activity is None:
        request.app.state.activity = ActivityCache(_compute)
    return request.app.state.activity


ActivityDep = Annotated[ActivityCache, Depends(get_activity)]


@router.get("/activity")
def activity(
    repo: RepoDep,
    activity_cache: ActivityDep,
    days: Annotated[int, Query(ge=1, le=366)] = 365,
) -> list[dict]:
    """The words written per day, oldest first, for the last `days` days."""
    return activity_cache.for_window(repo, date.today(), days)
