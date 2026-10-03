# -*- coding: utf-8 -*-
"""Recovering provenance a hand edit left stale.

Two halves, tested apart because only one of them needs git: `reconcile` replays a
paragraph's runs over its edited text, and `recover` is what finds the old text to
replay from. The split is the point — the rule about which character keeps which kind is
decided here with no repository anywhere near it.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

import pytest
from git import Repo

from inkspire_api import ink, provenance, repository
from tests.conftest import make_story

# §7.5's worked example, offsets and hashes both.
OLD = "The door creaked. The streets glistened like wet glass under the lamplight."
NEW = "The door creaked open. The streets glistened like wet glass under the lamplight."
OLD_RUNS = [(18, 45, "gen"), (45, 54, "fix"), (54, 75, "gen")]
NEW_RUNS = [(23, 50, "gen"), (50, 59, "fix"), (59, 80, "gen")]
OLD_HASH = "47f57caaa4fb330e"
NEW_HASH = "39f4ef4afdf22890"

RELPATH = PurePosixPath("stories/example-story/chapters/one.ink")


# --- the worked example ----------------------------------------------------


def test_the_example_in_the_design_is_what_this_produces() -> None:
    """§7.5's numbers, asserted rather than trusted: the two hashes it names and the
    runs it says come out. An insertion before every run shifts all of them by exactly
    its own length, and the inserted text is attributed to the writer."""
    assert provenance.paragraph_hash(OLD) == OLD_HASH
    assert provenance.paragraph_hash(NEW) == NEW_HASH
    assert len(OLD) == 75 and len(NEW) == 80
    assert provenance.reconcile(OLD, OLD_RUNS, NEW) == NEW_RUNS


def test_rehashing_instead_of_reconciling_is_the_silent_wrong_answer() -> None:
    """Why the hash is not simply recomputed. Kept against the new text, the old offsets
    start the `gen` run five characters early — on the `p` of a word the writer typed —
    and nothing anywhere would say so."""
    assert NEW[16:21] == " open"

    stale = provenance.expand_runs(OLD_RUNS, len(NEW))
    assert NEW[18] == "p" and stale[18] == "gen"

    reconciled = provenance.expand_runs(provenance.reconcile(OLD, OLD_RUNS, NEW), len(NEW))
    assert reconciled[18] == "user"
    assert NEW[23:50] == OLD[18:45]


# --- which character keeps which kind --------------------------------------


def test_unchanged_text_returns_its_runs_untouched() -> None:
    assert provenance.reconcile(OLD, OLD_RUNS, OLD) == OLD_RUNS


def test_an_insertion_before_a_run_shifts_every_boundary_by_the_same_amount() -> None:
    assert provenance.reconcile("abcdef", [(2, 4, "gen")], "XXabcdef") == [(4, 6, "gen")]


def test_an_insertion_inside_a_run_splits_it_in_two() -> None:
    """The inserted characters are the writer's, so the run cannot stay whole."""
    assert provenance.reconcile("abcdef", [(0, 6, "gen")], "abcXYdef") == [
        (0, 3, "gen"),
        (5, 8, "gen"),
    ]


def test_an_insertion_after_a_run_leaves_it_alone() -> None:
    assert provenance.reconcile("abcdef", [(0, 3, "gen")], "abcdefXY") == [(0, 3, "gen")]


def test_a_deletion_spanning_a_boundary_shortens_both_runs() -> None:
    """`cd` goes, taking one character off the end of `gen` and one off the start of
    `fix`; what is left of each keeps its own kind."""
    assert provenance.reconcile("abcdef", [(0, 3, "gen"), (3, 6, "fix")], "abef") == [
        (0, 2, "gen"),
        (2, 4, "fix"),
    ]


def test_replacing_a_whole_run_leaves_nothing_of_it() -> None:
    assert provenance.reconcile("abcdef", [(2, 4, "gen")], "abXYef") == []


def test_deleting_a_whole_run_leaves_nothing_of_it() -> None:
    assert provenance.reconcile("abcdef", [(2, 4, "gen")], "abef") == []


def test_a_run_survives_text_deleted_on_both_sides_of_it() -> None:
    assert provenance.reconcile("abcdef", [(2, 4, "gen")], "cd") == [(0, 2, "gen")]


def test_prose_with_no_runs_stays_with_no_runs() -> None:
    assert provenance.reconcile("abcdef", [], "abXYef") == []


def test_an_empty_paragraph_either_side_is_handled() -> None:
    assert provenance.reconcile("", [], "abc") == []
    assert provenance.reconcile("abc", [(0, 3, "gen")], "") == []


# --- expanding and collapsing ----------------------------------------------


def test_a_character_no_run_names_is_written_by_hand() -> None:
    assert provenance.expand_runs([(1, 3, "gen")], 5) == ["user", "gen", "gen", "user", "user"]


def test_collapsing_drops_the_user_stretches() -> None:
    assert provenance.collapse_runs(["user", "gen", "gen", "user"]) == [(1, 3, "gen")]
    assert provenance.collapse_runs(["user", "user"]) == []
    assert provenance.collapse_runs([]) == []


def test_neighbouring_characters_of_one_kind_become_one_run() -> None:
    assert provenance.collapse_runs(["gen", "gen", "fix", "fix"]) == [
        (0, 2, "gen"),
        (2, 4, "fix"),
    ]


def test_a_round_trip_normalises_runs_that_were_split() -> None:
    kinds = provenance.expand_runs([(0, 2, "gen"), (2, 5, "gen")], 5)
    assert provenance.collapse_runs(kinds) == [(0, 5, "gen")]


def test_a_run_reaching_past_the_text_is_clamped_rather_than_raising() -> None:
    """A file edited by hand can say anything. A load must not fail on it."""
    assert provenance.expand_runs([(2, 99, "gen")], 4) == ["user", "user", "gen", "gen"]
    assert provenance.expand_runs([(-5, 2, "gen")], 4) == ["gen", "gen", "user", "user"]
    assert provenance.expand_runs([(9, 20, "gen")], 4) == ["user"] * 4


def test_a_kind_this_build_does_not_know_is_carried_through() -> None:
    """A label, like an unknown section name. Dropping it would lose a newer build's
    record of something."""
    assert provenance.reconcile("abcdef", [(0, 3, "ghost")], "Xabcdef") == [(1, 4, "ghost")]


# --- without any history ---------------------------------------------------


def test_without_recovery_keeps_what_still_matches() -> None:
    body = "One.\n\nTwo.\n"
    first, second = provenance.hashes_of(body)
    answered = provenance.without_recovery(body, {first: [(0, 2, "gen")], "deadbeefdeadbeef": []})
    assert answered == {first: [(0, 2, "gen")], second: []}


def test_without_recovery_drops_a_key_whose_paragraph_is_gone() -> None:
    assert provenance.without_recovery("One.\n", {"deadbeefdeadbeef": [(0, 1, "gen")]}) == {
        provenance.paragraph_hash("One."): []
    }


def test_without_recovery_of_an_empty_body_is_empty() -> None:
    assert provenance.without_recovery("", {"deadbeefdeadbeef": []}) == {}


# --- recovering from history -----------------------------------------------


@pytest.fixture
def committed(git_root: Repo, data_root: Path) -> Repo:
    """A story whose one chapter holds §7.5's paragraph, committed."""
    make_story(data_root, "example-story", chapters={"one.ink": f"{ink.fence_line('body')}{OLD}\n"})
    git_root.index.add([str(RELPATH), "stories/example-story/story.yaml"])
    git_root.index.commit("Add the chapter")
    return git_root


def _edit(data_root: Path, body: str) -> None:
    """Rewrites the chapter's prose the way a hand edit outside the editor would."""
    (data_root / RELPATH).write_text(f"{ink.fence_line('body')}{body}\n", encoding="utf-8")


def test_a_match_in_an_older_revision_is_recovered(committed: Repo, data_root: Path) -> None:
    _edit(data_root, NEW)
    answered, revision = repository.recover(committed, RELPATH, NEW, {OLD_HASH: OLD_RUNS})
    assert answered == {NEW_HASH: NEW_RUNS}
    assert revision == committed.head.commit.hexsha[:7]


def test_no_match_in_any_revision_resets_that_paragraph(committed: Repo, data_root: Path) -> None:
    """The edited state was never committed, so nothing can say what the runs described.
    Nothing is invented."""
    _edit(data_root, NEW)
    answered, revision = repository.recover(
        committed, RELPATH, NEW, {"deadbeefdeadbeef": [(0, 4, "gen")]}
    )
    assert answered == {NEW_HASH: []}
    assert revision is None


def test_a_file_with_no_history_at_all_resets(git_root: Repo, data_root: Path) -> None:
    make_story(data_root, "example-story", chapters={"one.ink": f"{ink.fence_line('body')}{NEW}\n"})
    answered, revision = repository.recover(git_root, RELPATH, NEW, {OLD_HASH: OLD_RUNS})
    assert answered == {NEW_HASH: []}
    assert revision is None


def test_a_clean_file_makes_no_git_call(
    committed: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every hash matches, so there is nothing to look for. This is the common path and
    it must not pay for the rare one."""

    def refuse(*_args: object, **_kwargs: object) -> list[dict]:
        raise AssertionError("history was walked for a file with no stale paragraph")

    monkeypatch.setattr(repository, "_log_records", refuse)
    answered, revision = repository.recover(committed, RELPATH, OLD, {OLD_HASH: OLD_RUNS})
    assert answered == {OLD_HASH: OLD_RUNS}
    assert revision is None


def test_a_file_with_no_stored_provenance_makes_no_git_call(
    committed: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing stale, because nothing is stored. A chapter the editor has never saved."""

    def refuse(*_args: object, **_kwargs: object) -> list[dict]:
        raise AssertionError("history was walked for a file with no provenance")

    monkeypatch.setattr(repository, "_log_records", refuse)
    answered, revision = repository.recover(committed, RELPATH, NEW, {})
    assert answered == {NEW_HASH: []}
    assert revision is None


def test_only_the_stale_paragraph_is_recovered(committed: Repo, data_root: Path) -> None:
    """Its neighbours keep their own runs exactly, which is the whole reason provenance
    is keyed by paragraph and not by file."""
    before = f"Untouched.\n\n{OLD}\n"
    _edit(data_root, before)
    committed.index.add([str(RELPATH)])
    committed.index.commit("Two paragraphs")

    after = f"Untouched.\n\n{NEW}\n"
    _edit(data_root, after)
    kept = provenance.paragraph_hash("Untouched.")
    answered, revision = repository.recover(
        committed, RELPATH, after, {kept: [(0, 2, "fix")], OLD_HASH: OLD_RUNS}
    )
    assert answered == {kept: [(0, 2, "fix")], NEW_HASH: NEW_RUNS}
    assert revision == committed.head.commit.hexsha[:7]


def test_a_paragraph_too_unlike_the_stale_one_is_not_paired_with_it(
    committed: Repo, data_root: Path
) -> None:
    """The stored paragraph is in history, but nothing in the file now resembles it. A
    pairing here would be a guess, so the paragraph resets."""
    replaced = "Nothing whatsoever to do with any of that.\n"
    _edit(data_root, replaced)
    answered, revision = repository.recover(
        committed, RELPATH, replaced, {OLD_HASH: OLD_RUNS}
    )
    assert answered == {provenance.paragraph_hash(replaced.rstrip("\n")): []}
    assert revision == committed.head.commit.hexsha[:7]


def test_two_identical_stale_paragraphs_share_the_one_recovery(
    committed: Repo, data_root: Path
) -> None:
    """They hash the same, so they cannot hold different runs. The recovery has to be
    what they share — not an empty list because the second found the text already
    claimed."""
    after = f"{NEW}\n\n{NEW}\n"
    _edit(data_root, after)
    answered, revision = repository.recover(committed, RELPATH, after, {OLD_HASH: OLD_RUNS})
    assert answered == {NEW_HASH: NEW_RUNS}
    assert revision == committed.head.commit.hexsha[:7]


def test_the_walk_stops_after_the_revision_ceiling(
    committed: Repo, data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chapter with a long history and a paragraph stale since the start must not make
    one blob read per commit inside a GET."""
    monkeypatch.setattr(repository, "MAX_RECOVERY_REVISIONS", 2)
    for index in range(4):
        _edit(data_root, f"Revision {index}.\n")
        committed.index.add([str(RELPATH)])
        committed.index.commit(f"Edit {index}")

    _edit(data_root, NEW)
    answered, revision = repository.recover(committed, RELPATH, NEW, {OLD_HASH: OLD_RUNS})
    assert answered == {NEW_HASH: []}
    assert revision is None
