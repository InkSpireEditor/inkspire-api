# -*- coding: utf-8 -*-
"""The git routes: `/api/git/*`, and a chapter's `history` and `at/{rev}`."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from git import Repo

from inkspire_api.repository import NotARepository, open_repository
from tests.conftest import chapter_id, make_lorebook, make_story, make_timeline, story_id


def _committable_paths(changes: list[dict]) -> list[str]:
    return sorted(change["path"] for change in changes if change["committable"])


def _clone_and_push(git_origin: Path, tmp_path: Path, name: str, text: str) -> None:
    """Simulates another writer: clones the bare remote, adds one file, pushes it back."""
    clone_dir = tmp_path / f"clone-{name}"
    clone = Repo.clone_from(git_origin, clone_dir)
    with clone.config_writer() as writer:
        writer.set_value("user", "name", "John Smith")
        writer.set_value("user", "email", "john@example.com")
    (clone_dir / name).write_text(text, encoding="utf-8")
    clone.index.add([name])
    clone.index.commit(f"Add {name}")
    clone.remote().push("main:main")


# --- status -------------------------------------------------------------------


def test_a_clean_tree_has_nothing_to_show(logged_in: TestClient, git_root: Repo) -> None:
    body = logged_in.get("/api/git/status").json()
    assert body["branch"] == "main"
    assert body["upstream"] == "origin/main"
    assert body["ahead"] == 0
    assert body["behind"] == 0
    assert body["fetched"] is False
    assert body["clean"] is True
    assert body["changes"] == []


def test_a_chapter_is_committable_a_lorebook_and_a_timeline_are_not(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    """`/commit` only ever stages a story's manifest and its chapters. Everything else
    a story directory can hold shows as changed, and shows as not what the button
    is about to take."""
    story = make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    make_lorebook(story)
    make_timeline(story)
    git_root.git.add("-A")
    git_root.index.commit("Add a story with a lorebook and a timeline")

    (story / "chapters" / "one.ink").write_text("One, edited.", encoding="utf-8")
    (story / "lorebook" / "data" / "entities.yaml").write_text("x: 1\n", encoding="utf-8")
    (story / "timeline.yaml").write_text("title: Changed\n", encoding="utf-8")

    changes = logged_in.get("/api/git/status").json()["changes"]
    by_path = {change["path"]: change for change in changes}
    assert by_path["stories/example/chapters/one.ink"]["committable"] is True
    assert by_path["stories/example/lorebook/data/entities.yaml"]["committable"] is False
    assert by_path["stories/example/timeline.yaml"]["committable"] is False


def test_an_ignored_generated_file_never_shows(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    story = make_story(data_root, "example", title="Example Story")
    make_lorebook(story)
    git_root.git.add("-A")
    git_root.index.commit("Add a lorebook")

    build = story / "lorebook" / "build"
    build.mkdir()
    (build / "lorebook.ttl").write_text("", encoding="utf-8")

    assert logged_in.get("/api/git/status").json()["changes"] == []


def test_a_change_carries_the_story_it_is_in(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})

    changes = logged_in.get("/api/git/status").json()["changes"]
    assert changes[0]["dir"] == story_id(logged_in)


def test_a_detached_head_has_nothing_to_commit_onto(
    logged_in: TestClient, git_root: Repo
) -> None:
    git_root.git.checkout(git_root.head.commit.hexsha)
    response = logged_in.get("/api/git/status")
    assert response.status_code == 409


# --- commit -------------------------------------------------------------------


def test_committing_writes_only_the_committable_paths(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    story = make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    make_lorebook(story)
    git_root.git.add("-A")
    git_root.index.commit("Add a story with a lorebook")

    (story / "chapters" / "one.ink").write_text("One, edited.", encoding="utf-8")
    (story / "lorebook" / "data" / "entities.yaml").write_text("x: 1\n", encoding="utf-8")

    response = logged_in.post("/api/git/commit", json={"message": "Edit the opening"})
    assert response.status_code == 200
    body = response.json()
    assert body["committed"] == ["stories/example/chapters/one.ink"]
    assert body["message"] == "Edit the opening"

    # The lorebook edit is exactly as dirty as before the commit.
    after = logged_in.get("/api/git/status").json()
    assert _committable_paths(after["changes"]) == []
    assert len(after["changes"]) == 1
    assert after["changes"][0]["path"] == "stories/example/lorebook/data/entities.yaml"


def test_a_new_chapter_that_was_never_tracked_is_committed(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})

    response = logged_in.post("/api/git/commit", json={"message": "First chapter"})
    assert response.status_code == 200
    assert "stories/example/chapters/one.ink" in response.json()["committed"]
    assert logged_in.get("/api/git/status").json()["clean"] is True


def test_something_staged_by_hand_is_left_staged(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    """`--only` commits exactly the paths this API decided on, not whatever else a
    person staged in a shell alongside them."""
    story = make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    git_root.git.add("-A")
    git_root.index.commit("Add a story")

    (story / "chapters" / "one.ink").write_text("One, edited.", encoding="utf-8")
    (data_root / "by-hand.txt").write_text("not the API's", encoding="utf-8")
    git_root.git.add("by-hand.txt")

    response = logged_in.post("/api/git/commit", json={"message": "Edit the opening"})
    assert response.status_code == 200

    status_after = git_root.git.status("--porcelain")
    assert "by-hand.txt" in status_after
    assert "A  by-hand.txt" in status_after


def test_nothing_committable_is_refused(logged_in: TestClient, git_root: Repo) -> None:
    response = logged_in.post("/api/git/commit", json={"message": "Nothing changed"})
    assert response.status_code == 409


def test_an_overlong_message_is_refused_before_the_route_runs(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    response = logged_in.post("/api/git/commit", json={"message": "x" * 2001})
    assert response.status_code == 400


def test_committing_moves_the_branch_ahead_of_its_upstream(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    body = logged_in.post("/api/git/commit", json={"message": "First chapter"}).json()
    assert body["ahead"] == 1
    assert body["behind"] == 0


# --- push and pull --------------------------------------------------------------


def test_pushing_reaches_the_bare_remote(
    logged_in: TestClient, git_root: Repo, git_origin: Path, data_root: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    logged_in.post("/api/git/commit", json={"message": "First chapter"})

    response = logged_in.post("/api/git/push")
    assert response.status_code == 200
    body = response.json()
    assert body["ahead"] == 0
    assert body["behind"] == 0

    origin = Repo(git_origin)
    assert origin.heads.main.commit.hexsha == git_root.head.commit.hexsha


def test_pulling_fast_forwards_and_the_new_story_shows_up(
    logged_in: TestClient, git_root: Repo, git_origin: Path, data_root: Path, tmp_path: Path
) -> None:
    _clone_and_push(git_origin, tmp_path, "new-story-marker.txt", "placeholder")

    response = logged_in.post("/api/git/pull")
    assert response.status_code == 200
    body = response.json()
    assert body["ahead"] == 0
    assert body["behind"] == 0
    assert (data_root / "new-story-marker.txt").is_file()


def test_a_diverged_branch_is_refused_and_the_tree_is_untouched(
    logged_in: TestClient, git_root: Repo, git_origin: Path, data_root: Path, tmp_path: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    logged_in.post("/api/git/commit", json={"message": "First chapter"})
    before = git_root.head.commit.hexsha

    _clone_and_push(git_origin, tmp_path, "elsewhere.txt", "placeholder")

    response = logged_in.post("/api/git/pull")
    assert response.status_code == 409
    assert git_root.head.commit.hexsha == before
    assert git_root.git.status("--porcelain") == ""


def test_pushing_with_no_upstream_to_push_to_is_refused(
    logged_in: TestClient, git_root: Repo
) -> None:
    git_root.git.branch("--unset-upstream", "main")
    assert logged_in.post("/api/git/push").status_code == 409


# --- the lock -------------------------------------------------------------------


def test_a_second_git_change_while_one_is_running_is_refused(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    lock = logged_in.app.state.git_lock
    assert lock.acquire(blocking=False)
    try:
        response = logged_in.post("/api/git/commit", json={"message": "First chapter"})
        assert response.status_code == 409
    finally:
        lock.release()

    # The lock was released, so the same request now goes through.
    assert logged_in.post("/api/git/commit", json={"message": "First chapter"}).status_code == 200


# --- opening the repository -----------------------------------------------------


def test_no_repository_at_all_is_a_500(logged_in: TestClient, data_root: Path) -> None:
    """`data_root` alone, without `git_root`, is a plain directory: no `.git`."""
    assert logged_in.get("/api/git/status").status_code == 500


def test_a_root_that_is_not_a_repository_is_refused(tmp_path: Path) -> None:
    with pytest.raises(NotARepository):
        with open_repository(tmp_path / "not-a-repository"):
            pass


def test_a_root_one_level_inside_a_repository_does_not_reach_it(tmp_path: Path) -> None:
    """A misconfigured root one level too deep must not silently open the repository
    above it -- this project's own source tree included, were `INKSPIRE_DATA_ROOT`
    ever left at its relative default."""
    outer = tmp_path / "repository"
    (outer / "stories").mkdir(parents=True)
    Repo.init(outer)

    with pytest.raises(NotARepository):
        with open_repository(outer / "stories"):
            pass


# --- history ---------------------------------------------------------------------


@pytest.fixture
def story_with_history(git_root: Repo, data_root: Path) -> Path:
    """A chapter committed, renamed, and edited again, each its own commit."""
    story = make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    git_root.git.add("-A")
    git_root.index.commit("First chapter")

    git_root.git.mv(
        "stories/example/chapters/one.ink", "stories/example/chapters/renamed.ink"
    )
    git_root.index.commit("Rename the chapter")

    (story / "chapters" / "renamed.ink").write_text("One, edited.", encoding="utf-8")
    git_root.git.add("-A")
    git_root.index.commit("Edit the opening")
    return story


def test_history_follows_a_chapter_across_a_rename(
    logged_in: TestClient, story_with_history: Path
) -> None:
    chapter = chapter_id(logged_in, "Example Story", "renamed")
    records = logged_in.get(f"/api/stories/file/{chapter}/history").json()
    assert [record["message"] for record in records] == [
        "Edit the opening",
        "Rename the chapter",
        "First chapter",
    ]


def test_history_caps_at_the_requested_limit(
    logged_in: TestClient, story_with_history: Path
) -> None:
    chapter = chapter_id(logged_in, "Example Story", "renamed")
    records = logged_in.get(f"/api/stories/file/{chapter}/history?limit=1").json()
    assert len(records) == 1
    assert records[0]["message"] == "Edit the opening"


# --- at one revision --------------------------------------------------------------


def test_content_at_a_revision_from_before_the_rename(
    logged_in: TestClient, story_with_history: Path
) -> None:
    chapter = chapter_id(logged_in, "Example Story", "renamed")
    records = logged_in.get(f"/api/stories/file/{chapter}/history").json()
    first_commit = records[-1]["sha"]

    response = logged_in.get(f"/api/stories/file/{chapter}/at/{first_commit}")
    assert response.status_code == 200
    assert response.text == "One."


def test_a_revision_outside_the_chapters_history_is_not_found(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    git_root.git.add("-A")
    git_root.index.commit("Add the first chapter")

    make_story(data_root, "other", title="Other Story", chapters={"two.ink": "Two."})
    git_root.git.add("-A")
    other_commit = git_root.index.commit("Add the second story").hexsha

    # `other_commit` never touched "one.ink", so it is not in that chapter's own log.
    one = chapter_id(logged_in, "Example Story", "one")
    response = logged_in.get(f"/api/stories/file/{one}/at/{other_commit}")
    assert response.status_code == 404


def test_a_rev_that_is_not_a_hex_sha_is_refused_before_the_route_runs(
    logged_in: TestClient, git_root: Repo, data_root: Path
) -> None:
    make_story(data_root, "example", title="Example Story", chapters={"one.ink": "One."})
    chapter = chapter_id(logged_in, "Example Story", "one")
    response = logged_in.get(f"/api/stories/file/{chapter}/at/not-a-sha")
    assert response.status_code == 400


# --- authentication ----------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/api/git/status"),
        ("post", "/api/git/commit"),
        ("post", "/api/git/push"),
        ("post", "/api/git/pull"),
    ],
)
def test_the_git_routes_refuse_a_request_with_no_token(
    client: TestClient, git_root: Repo, method: str, path: str
) -> None:
    assert getattr(client, method)(path).status_code == 401


@pytest.mark.parametrize("path", ["history", "at/0000000"])
def test_the_history_routes_refuse_a_request_with_no_token(
    client: TestClient, git_root: Repo, path: str
) -> None:
    assert client.get(f"/api/stories/file/anything/{path}").status_code == 401
