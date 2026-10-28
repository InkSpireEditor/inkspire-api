# -*- coding: utf-8 -*-
"""The words written per day: the counting rule, the grouping, and the route."""

from __future__ import annotations

import datetime
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from git import Actor, Repo

from inkspire_api.activity import MAX_DIFF_WORDS, body_of, by_day, words_added
from inkspire_api.repository import NameStatus, history_records, parse_name_status
from tests.conftest import make_story

TODAY = datetime.date(2026, 10, 28)


# --- the counting rule --------------------------------------------------------


def test_an_insertion_counts_its_words() -> None:
    assert words_added("", "one two three") == 3


def test_a_deletion_counts_nothing() -> None:
    assert words_added("one two three", "") == 0


def test_identical_texts_count_nothing() -> None:
    assert words_added("one two", "one two") == 0


def test_two_empty_texts_count_nothing() -> None:
    assert words_added("", "") == 0


def test_a_replaced_paragraph_counts_only_its_new_words() -> None:
    """Changing 3 words of 10 counts 3, not the whole paragraph."""
    old = "a b c d e f g h i j"
    new = "a b c d e f g X Y Z"
    assert words_added(old, new) == 3


def test_a_rewrite_to_the_same_length_counts_its_words() -> None:
    """The words a commit writes are the words it adds, not the net gain: a
    hundred rewritten words count a hundred even when the passage ends up the
    same length."""
    assert words_added("one hundred red balloons float", "ten thousand blue kites soar") == 5


def test_a_paragraph_moved_without_change_counts_its_words() -> None:
    """A move is not distinguished from a rewrite: the moved paragraph is a
    replacement, and its words are counted the same way."""
    moved = words_added("first para\n\nsecond para", "second para\n\nfirst para")
    assert moved == 2


def test_a_run_over_the_limit_counts_all_its_new_words() -> None:
    """A word-level diff of a whole rewritten chapter is quadratic, and a
    rewrite that large is counted in full anyway. Finishing at all is the point:
    a quadratic diff of 5000 words would not."""
    old = " ".join(f"w{i}" for i in range(MAX_DIFF_WORDS + 2000))
    new = " ".join(f"w{i + 1}" for i in range(MAX_DIFF_WORDS + 2000))
    assert words_added(old, new) == MAX_DIFF_WORDS + 2000


def test_body_of_leaves_out_every_section() -> None:
    text = (
        "===== ink:meta\ntitle: A Long Title\nstatus: draft\n"
        "===== ink:body\nalpha beta\n"
        '===== ink:provenance\n"47f57caaa4fb330e": [[0, 5, "gen"]]\n'
        "===== ink:context_summary\nWhat was trimmed.\n"
    )
    assert body_of(text) == "alpha beta\n"


def test_body_of_a_file_with_no_sections_is_the_whole_file() -> None:
    assert body_of("one two three") == "one two three"


def test_body_of_nothing_is_nothing() -> None:
    assert body_of("") == ""


# --- the grouping -------------------------------------------------------------


def test_days_are_grouped_and_sorted_oldest_first() -> None:
    records = [
        (TODAY, 40),
        (TODAY - datetime.timedelta(days=1), 100),
        (TODAY, 60),
    ]
    assert by_day(records, TODAY, 30) == [
        {"date": "2026-10-27", "words": 100},
        {"date": "2026-10-28", "words": 100},
    ]


def test_a_day_outside_the_window_is_dropped() -> None:
    records = [(TODAY - datetime.timedelta(days=10), 500)]
    assert by_day(records, TODAY, 7) == []


def test_a_day_with_no_words_is_absent() -> None:
    records = [(TODAY, 0), (TODAY - datetime.timedelta(days=1), 12)]
    assert by_day(records, TODAY, 30) == [
        {"date": "2026-10-27", "words": 12}
    ]


def test_the_window_includes_its_first_day() -> None:
    records = [(TODAY - datetime.timedelta(days=364), 5)]
    assert by_day(records, TODAY, 365) == [
        {"date": (TODAY - datetime.timedelta(days=364)).isoformat(), "words": 5}
    ]


# --- reading git's output -----------------------------------------------------


def test_parse_name_status_reads_one_entry_per_status() -> None:
    raw = "A\0stories/s/chapters/a.ink\0M\0stories/s/story.yaml\0D\0stories/s/chapters/b.ink\0"
    assert parse_name_status(raw) == [
        NameStatus("A", None, "stories/s/chapters/a.ink"),
        NameStatus("M", "stories/s/story.yaml", "stories/s/story.yaml"),
        NameStatus("D", "stories/s/chapters/b.ink", None),
    ]


def test_parse_name_status_reads_both_paths_of_a_rename() -> None:
    raw = "R092\0stories/s/chapters/old.ink\0stories/s/chapters/new.ink\0A\0stories/x.ink\0"
    assert parse_name_status(raw) == [
        NameStatus("R092", "stories/s/chapters/old.ink", "stories/s/chapters/new.ink"),
        NameStatus("A", None, "stories/x.ink"),
    ]


def test_parse_name_status_of_nothing_is_nothing() -> None:
    assert parse_name_status("") == []


# --- a real repository ----------------------------------------------------------


def at(days_ago: int, clock: str = "12:00:00", offset: str = "+0000") -> str:
    """An author date `days_ago` days before today, at `clock` in the zone `offset`.

    Noon by default, so a test never straddles midnight whatever the zone."""
    day = datetime.date.today() - datetime.timedelta(days=days_ago)
    return f"{day.isoformat()} {clock} {offset}"


def day_of(days_ago: int) -> str:
    return (datetime.date.today() - datetime.timedelta(days=days_ago)).isoformat()


def commit(repo: Repo, files: dict[str, str | bytes | None], when: str, message: str = "Work") -> None:
    """Writes `files` (a path to its text, bytes, or `None` to delete it), stages them
    and commits with the author date `when`, applied the way git itself applies one,
    through the environment."""
    root = Path(str(repo.working_tree_dir))
    for path, content in files.items():
        full = root / path
        if content is None:
            full.unlink()
        else:
            full.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                full.write_bytes(content)
            else:
                full.write_text(content, encoding="utf-8")
    repo.git.add("-A")
    identity = Actor.committer(repo.config_reader())
    with repo.git.custom_environment(GIT_AUTHOR_DATE=when):
        repo.git.commit("-m", message, "--author", f"{identity.name} <{identity.email}>")


def activity(client: TestClient, days: int = 30) -> list[dict]:
    response = client.get(f"/api/stories/activity?days={days}")
    assert response.status_code == 200
    return response.json()


CHAPTERS = "stories/example-story/chapters"


@pytest.fixture
def story_repo(git_root: Repo, data_root: Path) -> Repo:
    """A repository with one committed story: two chapters and a one-shot.

    The commit is dated outside every window the tests ask for, so only the
    words a test itself commits appear in an answer."""
    make_story(
        data_root,
        "example-story",
        title="Example Story",
        chapters={"one.ink": "one two three four five", "two.ink": "alpha beta gamma"},
    )
    (data_root / "stories" / "solo.ink").write_text("solo words here", encoding="utf-8")
    git_root.git.add("-A")
    with git_root.git.custom_environment(GIT_AUTHOR_DATE=at(60)):
        git_root.git.commit("-m", "Add a story")
    return git_root


def test_a_new_chapter_counts_all_its_words(logged_in: TestClient, story_repo: Repo) -> None:
    commit(story_repo, {f"{CHAPTERS}/three.ink": "one two three"}, at(2))

    assert activity(logged_in) == [{"date": day_of(2), "words": 3}]


def test_an_edit_counts_only_added_words(logged_in: TestClient, story_repo: Repo) -> None:
    """Changing one word of five counts 1, not 5."""
    commit(story_repo, {f"{CHAPTERS}/one.ink": "one two three four X"}, at(1))

    assert activity(logged_in) == [{"date": day_of(1), "words": 1}]


def test_a_commit_of_several_files_counts_every_chapter_in_it(
    logged_in: TestClient, story_repo: Repo
) -> None:
    """Git prints a commit's entries one after another; a chapter must not be lost
    because of the one before it, and a manifest in the same commit adds nothing."""
    commit(
        story_repo,
        {
            f"{CHAPTERS}/three.ink": "one two three",
            f"{CHAPTERS}/four.ink": "four five",
            f"{CHAPTERS}/five.ink": "six",
            "stories/example-story/story.yaml": "title: Example Story\nsynopsis: Changed.\n",
        },
        at(1),
    )

    assert activity(logged_in) == [{"date": day_of(1), "words": 6}]


def test_the_root_commit_counts(tmp_path: Path) -> None:
    """The first commit of a repository has no parent; every file in it is new."""
    repo = Repo.init(tmp_path / "fresh", initial_branch="main")
    with repo.config_writer() as writer:
        writer.set_value("user", "name", "Jane Doe")
        writer.set_value("user", "email", "jane@example.com")
    commit(repo, {"stories/solo.ink": "one two three four"}, at(3), "Root")

    records = list(history_records(repo, datetime.date.today() - datetime.timedelta(days=30)))

    assert [day.isoformat() for day, _ in records] == [day_of(3)]
    assert [words_added(body_of(old), body_of(new)) for _, changes in records for old, new in changes] == [4]


def test_two_commits_on_one_day_sum(logged_in: TestClient, story_repo: Repo) -> None:
    commit(story_repo, {f"{CHAPTERS}/three.ink": "one two"}, at(1))
    commit(story_repo, {f"{CHAPTERS}/three.ink": "one two\n\nthree four"}, at(1))

    assert activity(logged_in) == [{"date": day_of(1), "words": 4}]


def test_commits_on_different_days_are_separate_entries(
    logged_in: TestClient, story_repo: Repo
) -> None:
    commit(story_repo, {f"{CHAPTERS}/three.ink": "one"}, at(3))
    commit(story_repo, {f"{CHAPTERS}/four.ink": "two"}, at(1))

    assert activity(logged_in) == [
        {"date": day_of(3), "words": 1},
        {"date": day_of(1), "words": 1},
    ]


@pytest.mark.parametrize(
    "clock, offset",
    [
        # 00:30 at +01:00 is 23:30 the day before in UTC: converting would move the day.
        ("00:30:00", "+0100"),
        # 23:30 at -05:00 is 04:30 the day after in UTC.
        ("23:30:00", "-0500"),
    ],
)
def test_an_author_date_stays_on_the_day_it_was_written_on(
    logged_in: TestClient, story_repo: Repo, clock: str, offset: str
) -> None:
    commit(story_repo, {f"{CHAPTERS}/three.ink": "one two"}, at(2, clock, offset))

    assert activity(logged_in) == [{"date": day_of(2), "words": 2}]


def test_a_commit_with_no_ink_change_adds_nothing(logged_in: TestClient, story_repo: Repo) -> None:
    commit(story_repo, {"stories/example-story/timeline.yaml": "title: Example\n"}, at(1))

    assert activity(logged_in) == []


def test_a_one_shot_counts(logged_in: TestClient, story_repo: Repo) -> None:
    commit(story_repo, {"stories/solo.ink": "solo words here and more"}, at(1))

    assert activity(logged_in) == [{"date": day_of(1), "words": 2}]


def test_a_rename_without_an_edit_counts_nothing(logged_in: TestClient, story_repo: Repo) -> None:
    root = Path(str(story_repo.working_tree_dir))
    (root / CHAPTERS / "one.ink").rename(root / CHAPTERS / "renamed.ink")
    commit(story_repo, {}, at(1), "Rename a chapter")

    assert activity(logged_in) == []


def test_a_rename_that_also_edits_counts_only_what_it_adds(
    logged_in: TestClient, story_repo: Repo
) -> None:
    """Git pairs a deleted file with an added one as a rename only when they are alike
    and long enough to compare, so the chapter is made long first."""
    lines = "".join(f"line {n} alpha beta gamma delta\n" for n in range(40))
    commit(story_repo, {f"{CHAPTERS}/long.ink": lines}, at(5))
    commit(story_repo, {f"{CHAPTERS}/long.ink": None, f"{CHAPTERS}/renamed.ink": lines + "extra\n"}, at(1))

    assert activity(logged_in) == [
        {"date": day_of(5), "words": 40 * 6},
        {"date": day_of(1), "words": 1},
    ]


def test_a_deleted_chapter_counts_nothing(logged_in: TestClient, story_repo: Repo) -> None:
    commit(story_repo, {f"{CHAPTERS}/one.ink": None}, at(1))

    assert activity(logged_in) == []


def test_a_lorebook_and_a_timeline_do_not_count(logged_in: TestClient, story_repo: Repo) -> None:
    commit(
        story_repo,
        {
            "stories/example-story/timeline.yaml": "title: Example\n",
            "stories/example-story/lorebook/lorebook.yaml": "title: Example\n",
        },
        at(1),
    )

    assert activity(logged_in) == []


def test_a_file_that_is_not_text_adds_nothing_and_does_not_stop_the_count(
    logged_in: TestClient, story_repo: Repo
) -> None:
    commit(
        story_repo,
        {f"{CHAPTERS}/binary.ink": b"\xff\xfe\x00\x01", f"{CHAPTERS}/three.ink": "one two"},
        at(1),
    )

    assert activity(logged_in) == [{"date": day_of(1), "words": 2}]


def test_a_commit_older_than_the_window_is_not_in_the_answer(
    logged_in: TestClient, story_repo: Repo
) -> None:
    commit(story_repo, {f"{CHAPTERS}/three.ink": "one two"}, at(40))

    assert activity(logged_in, 30) == []


def test_only_the_newest_commits_up_to_the_cap_are_read(
    logged_in: TestClient, story_repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("inkspire_api.repository.MAX_ACTIVITY_COMMITS", 1)
    commit(story_repo, {f"{CHAPTERS}/three.ink": "one"}, at(5))
    commit(story_repo, {f"{CHAPTERS}/four.ink": "two three"}, at(2))

    assert activity(logged_in) == [{"date": day_of(2), "words": 2}]


# --- sections are not prose -----------------------------------------------------


SECTIONED = (
    "===== ink:meta\ntitle: A Long Title Of Many Words\nstatus: draft\n"
    "===== ink:body\nalpha beta gamma delta\n"
    '===== ink:provenance\n"47f57caaa4fb330e": [[0, 5, "gen"], [6, 10, "fix"]]\n'
)


def test_a_chapter_with_sections_counts_its_prose_only(
    logged_in: TestClient, story_repo: Repo
) -> None:
    commit(story_repo, {f"{CHAPTERS}/sectioned.ink": SECTIONED}, at(3))

    assert activity(logged_in) == [{"date": day_of(3), "words": 4}]


def test_a_commit_that_only_changes_a_section_adds_nothing(
    logged_in: TestClient, story_repo: Repo
) -> None:
    commit(story_repo, {f"{CHAPTERS}/sectioned.ink": SECTIONED}, at(3))
    commit(
        story_repo,
        {
            f"{CHAPTERS}/sectioned.ink": SECTIONED.replace(
                '[[0, 5, "gen"], [6, 10, "fix"]]',
                '[[0, 5, "gen"], [6, 10, "fix"], [11, 15, "gen"]]',
            ).replace("title: A Long Title Of Many Words", "title: Another Longer Title Still"),
        },
        at(1),
    )

    assert activity(logged_in) == [{"date": day_of(3), "words": 4}]


# --- the route -----------------------------------------------------------------


def test_the_route_needs_a_token(client: TestClient, git_root: Repo) -> None:
    assert client.get("/api/stories/activity").status_code == 401


def test_the_route_answers_a_list_of_dates_and_words(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "one two"})
    git_root.git.add("-A")
    git_root.index.commit("Add a story")

    body = activity(logged_in)
    assert len(body) == 1
    assert set(body[0]) == {"date", "words"}
    assert body[0]["words"] == 2


def test_days_out_of_range_is_a_400(logged_in: TestClient, git_root: Repo) -> None:
    for days in ("0", "367", "x"):
        response = logged_in.get(f"/api/stories/activity?days={days}")
        assert response.status_code == 400
        assert "message" in response.json()


def test_days_seven_returns_only_the_last_seven_days(
    logged_in: TestClient, story_repo: Repo
) -> None:
    for days_ago, words in ((20, "one"), (10, "two"), (2, "three four")):
        commit(story_repo, {f"{CHAPTERS}/{days_ago}.ink": words}, at(days_ago))

    assert activity(logged_in, 7) == [{"date": day_of(2), "words": 2}]


def test_a_second_request_makes_no_new_git_call(
    logged_in: TestClient, story_repo: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer is held against `HEAD` and today, so a second request inside
    the window slices the held one."""
    calls = 0
    reader = history_records

    def counting(repo, since):
        nonlocal calls
        calls += 1
        return reader(repo, since)

    monkeypatch.setattr("inkspire_api.activity.history_records", counting)

    activity(logged_in)
    assert calls == 1
    activity(logged_in, 7)
    assert calls == 1


def test_a_new_commit_makes_the_next_request_see_it(
    logged_in: TestClient, story_repo: Repo
) -> None:
    assert activity(logged_in) == []

    commit(story_repo, {f"{CHAPTERS}/three.ink": "one two"}, at(1))

    assert activity(logged_in) == [{"date": day_of(1), "words": 2}]


def test_a_data_root_that_is_not_a_repository_answers_the_same_error(
    logged_in: TestClient, git_root: Repo, tmp_path: Path, settings
) -> None:
    """The settings' data root is what the route reads, so a root that is not a
    git tree answers what `/api/git/status` answers."""
    from inkspire_api.deps import get_settings

    logged_in.app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"data_root": tmp_path / "elsewhere"}
    )

    git_error = logged_in.get("/api/git/status")
    activity_error = logged_in.get("/api/stories/activity")
    assert activity_error.status_code == git_error.status_code
    assert activity_error.json() == git_error.json()


def test_a_repository_with_no_commits_answers_an_empty_list(
    logged_in: TestClient, git_root: Repo, data_root: Path, settings
) -> None:
    """A repository with no commits has written nothing. A root that is not a git
    tree at all is the `NotARepository` error, the same as `/api/git/*`."""
    from inkspire_api.deps import get_settings

    fresh = data_root / "fresh"
    shutil.rmtree(fresh, ignore_errors=True)
    (fresh / "stories").mkdir(parents=True)
    Repo.init(fresh, initial_branch="main")

    logged_in.app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"data_root": fresh}
    )
    assert activity(logged_in) == []
