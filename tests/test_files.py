# -*- coding: utf-8 -*-
"""The story routes, over a story repository built for each test.

A story is a directory and its chapters are its files, so `/api/stories/dir` operates on
`stories/<slug>/` and `/api/stories/file` on the `.ink` files under it. The other root,
under `/api/notes`, is in `test_notes_routes.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from inkspire_api.fs import MAX_FILE_BYTES
from inkspire_api.models import User
from inkspire_api.security import hash_password

from tests.conftest import (
    EMAIL,
    chapter_id,
    dir_named,
    entry_named,
    make_story,
    story_id,
)

CONTENT_TYPE = {"Content-Type": "text/plain"}


@pytest.fixture
def repository(data_root: Path) -> Path:
    """Two stories: one with two chapters, one empty."""
    make_story(
        data_root,
        "example-story",
        title="Example Story",
        synopsis="In one line.",
        chapters={"first-chapter.ink": "Once.", "second-chapter.ink": ""},
    )
    make_story(data_root, "other-story", title="Other Story")
    return data_root


def dirs(client: TestClient) -> list[dict]:
    return client.get("/api/stories/tree").json()["dirs"]


def names(listing: list[dict]) -> list[str]:
    """The names in a listing, in the order it gives them."""
    return [entry["name"] for entry in listing]


def ids(listing: list[dict]) -> list[str]:
    """The ids in a listing, for a test asking only whether something is in it."""
    return [entry["id"] for entry in listing]


def story_files(client: TestClient, story: str) -> list[dict]:
    """One story's chapters, from the per-story route rather than from the tree."""
    return client.get(f"/api/stories/dir/{story_id(client, story)}").json()["files"]


def manifest(data_root: Path, slug: str) -> dict:
    return yaml.safe_load(
        (data_root / "stories" / slug / "story.yaml").read_text(encoding="utf-8")
    )


# --- the tree --------------------------------------------------------------


def test_the_tree_needs_a_token(client: TestClient, repository: Path) -> None:
    response = client.get("/api/stories/tree")
    assert response.status_code == 401
    assert response.json() == {"code": 401, "message": "JWT Token not found"}


def test_the_tree_lists_every_story(logged_in: TestClient, repository: Path) -> None:
    body = logged_in.get("/api/stories/tree").json()

    assert body["user"] == EMAIL
    assert names(body["dirs"]) == ["Example Story", "Other Story"]
    assert entry_named(body["dirs"], "Example Story") == {
        "id": story_id(logged_in, "Example Story"),
        "name": "Example Story",
        "summary": "In one line.",
        "timeline": False,
        "lorebook": False,
        "files": [
            {"id": chapter_id(logged_in, "Example Story", "first-chapter"),
             "name": "first-chapter", "status": ""},
            {"id": chapter_id(logged_in, "Example Story", "second-chapter"),
             "name": "second-chapter", "status": ""},
        ],
    }


def test_the_tree_carries_every_story_s_chapters_in_one_request(
    logged_in: TestClient, repository: Path
) -> None:
    """The whole point of the shape: no request per directory to find out what is in it."""
    dirs_ = dirs(logged_in)

    assert names(entry_named(dirs_, "Example Story")["files"]) == [
        "first-chapter",
        "second-chapter",
    ]
    assert entry_named(dirs_, "Other Story")["files"] == []


def test_the_tree_says_which_further_views_a_story_has(
    logged_in: TestClient, data_root: Path
) -> None:
    """So a client can offer a timeline or a lore view without fetching either."""
    story = make_story(data_root, "example-story", title="Example Story")
    (story / "timeline.yaml").write_text("title: Example\n", encoding="utf-8")

    entry = entry_named(dirs(logged_in), "Example Story")
    assert entry["timeline"] is True
    assert entry["lorebook"] is False


def test_the_tree_leaves_out_a_chapter_s_summary(
    logged_in: TestClient, data_root: Path
) -> None:
    """It covers every story, so the longest field is on `dir/{id}` instead."""
    make_story(
        data_root,
        "example-story",
        title="Example Story",
        chapters={"first-chapter.ink": "---\nsummary: What happens.\n---\nOnce.\n"},
    )

    listed = entry_named(dirs(logged_in), "Example Story")["files"][0]
    assert "summary" not in listed
    assert story_files(logged_in, "Example Story")[0]["summary"] == "What happens."


def test_the_tree_has_no_loose_files(logged_in: TestClient, repository: Path) -> None:
    """Every chapter belongs to a story, so nothing sits at the root of this space.

    A file that belongs to no story lives in the other root. See `test_notes_routes.py`.
    """
    assert logged_in.get("/api/stories/tree").json()["files"] == []


def test_an_empty_repository_is_an_empty_tree(logged_in: TestClient, data_root: Path) -> None:
    assert logged_in.get("/api/stories/tree").json() == {"user": EMAIL, "files": [], "dirs": []}


def test_a_repository_that_is_not_there_is_an_empty_tree(logged_in: TestClient) -> None:
    """An installation that has not cloned the stories yet still serves the app."""
    assert logged_in.get("/api/stories/tree").json()["dirs"] == []


# --- one story -------------------------------------------------------------


def test_a_story_lists_its_chapters(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    body = logged_in.get(f"/api/stories/dir/{identifier}").json()

    assert body["id"] == identifier
    assert body["name"] == "Example Story"
    assert body["summary"] == "In one line."
    assert names(body["files"]) == ["first-chapter", "second-chapter"]


def test_an_unknown_story_is_not_found(logged_in: TestClient, repository: Path) -> None:
    response = logged_in.get(f"/api/stories/dir/{'0' * 16}")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_a_story_needs_a_token(client: TestClient, repository: Path) -> None:
    assert client.get(f"/api/stories/dir/{'0' * 16}").status_code == 401


# --- creating --------------------------------------------------------------


def test_creating_a_story_writes_a_directory(logged_in: TestClient, data_root: Path) -> None:
    response = logged_in.post(
        "/api/stories/dir", json={"name": "Example Story", "summary": "In one line."}
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Example Story"
    assert manifest(data_root, "example-story") == {
        "title": "Example Story",
        "synopsis": "In one line.",
        "chapters": [],
    }


def test_a_created_story_is_in_the_tree(logged_in: TestClient, data_root: Path) -> None:
    created = logged_in.post("/api/stories/dir", json={"name": "Example Story"}).json()
    assert created["id"] in ids(dirs(logged_in))


def test_creating_a_story_with_no_summary_is_allowed(logged_in: TestClient, data_root: Path) -> None:
    response = logged_in.post("/api/stories/dir", json={"name": "Example Story"})
    assert response.status_code == 201
    assert response.json()["summary"] == ""


def test_two_stories_may_share_a_name(logged_in: TestClient, data_root: Path) -> None:
    first = logged_in.post("/api/stories/dir", json={"name": "Example Story"}).json()
    second = logged_in.post("/api/stories/dir", json={"name": "Example Story"}).json()

    assert first["id"] != second["id"]
    assert len(dirs(logged_in)) == 2


def test_a_story_needs_a_name(logged_in: TestClient, data_root: Path) -> None:
    assert logged_in.post("/api/stories/dir", json={"name": "   "}).status_code == 400
    assert logged_in.post("/api/stories/dir", json={}).status_code == 400


def test_a_story_name_has_a_limit(logged_in: TestClient, data_root: Path) -> None:
    response = logged_in.post("/api/stories/dir", json={"name": "x" * 256})
    assert response.status_code == 400
    assert "255" in response.json()["message"]


def test_a_synopsis_has_a_limit(logged_in: TestClient, data_root: Path) -> None:
    response = logged_in.post(
        "/api/stories/dir", json={"name": "Example Story", "summary": "x" * 2001}
    )
    assert response.status_code == 400
    assert "2000" in response.json()["message"]


def test_creating_a_chapter_writes_an_ink_file(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    response = logged_in.post(
        "/api/stories/file", json={"name": "Third Chapter", "dir": identifier}
    )

    assert response.status_code == 201
    assert response.json()["name"] == "Third Chapter"
    assert response.json()["dir"] == identifier
    path = repository / "stories" / "example-story" / "chapters" / "third-chapter.ink"
    assert path.read_text(encoding="utf-8") == "---\ntitle: Third Chapter\n---\n"


def test_a_created_chapter_is_in_its_story(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    created = logged_in.post(
        "/api/stories/file", json={"name": "Third Chapter", "dir": identifier}
    ).json()

    listing = logged_in.get(f"/api/stories/dir/{identifier}").json()["files"]
    assert entry_named(listing, "Third Chapter")["id"] == created["id"]


def test_a_chapter_cannot_be_created_outside_a_story(logged_in: TestClient, repository: Path) -> None:
    """`dir: null` is the root, and this space has none. Under `/api/notes` it is allowed."""
    response = logged_in.post("/api/stories/file", json={"name": "Loose", "dir": None})
    assert response.status_code == 422
    assert "story" in response.json()["message"]


def test_a_chapter_in_an_unknown_story_is_not_found(logged_in: TestClient, repository: Path) -> None:
    response = logged_in.post("/api/stories/file", json={"name": "Third", "dir": "0" * 16})
    assert response.status_code == 404


def test_a_chapter_needs_a_name(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    response = logged_in.post("/api/stories/file", json={"name": " ", "dir": identifier})
    assert response.status_code == 400


# --- renaming and retitling -------------------------------------------------


def test_retitling_a_story_keeps_its_id(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    response = logged_in.put(
        f"/api/stories/dir/{identifier}", json={"name": "Renamed", "summary": "Rewritten."}
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": identifier,
        "name": "Renamed",
        "summary": "Rewritten.",
    }
    assert manifest(repository, "example-story")["title"] == "Renamed"


def test_retitling_a_story_leaves_its_chapters_reachable(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")

    logged_in.put(f"/api/stories/dir/{identifier}", json={"name": "Renamed"})

    assert logged_in.get(f"/api/stories/file/{chapter}").status_code == 200


def test_rewriting_only_the_synopsis_keeps_the_title(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    response = logged_in.put(f"/api/stories/dir/{identifier}", json={"summary": "Rewritten."})

    assert response.json() == {
        "id": identifier,
        "name": "Example Story",
        "summary": "Rewritten.",
    }


def test_retitling_an_unknown_story_is_not_found(logged_in: TestClient, repository: Path) -> None:
    response = logged_in.put(f"/api/stories/dir/{'0' * 16}", json={"name": "Renamed"})
    assert response.status_code == 404


def test_renaming_a_chapter_renames_its_file(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.put(f"/api/stories/file/{chapter}", json={"name": "Renamed Chapter"})

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed Chapter"

    chapters = repository / "stories" / "example-story" / "chapters"
    assert (chapters / "renamed-chapter.ink").read_text(encoding="utf-8") == (
        "---\ntitle: Renamed Chapter\n---\nOnce."
    )
    assert not (chapters / "first-chapter.ink").exists()


def test_a_renamed_chapter_answers_under_its_new_id(logged_in: TestClient, repository: Path) -> None:
    """The id is derived from the path, so a rename issues a new one."""
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    renamed = logged_in.put(f"/api/stories/file/{chapter}", json={"name": "Renamed"}).json()

    assert renamed["id"] != chapter
    assert logged_in.get(f"/api/stories/file/{renamed['id']}").json()["name"] == "Renamed"
    assert logged_in.get(f"/api/stories/file/{chapter}").status_code == 404


def test_renaming_an_unknown_chapter_is_not_found(logged_in: TestClient, repository: Path) -> None:
    response = logged_in.put(f"/api/stories/file/{'0' * 16}", json={"name": "Renamed"})
    assert response.status_code == 404


def test_moving_a_chapter_to_another_story(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    target = story_id(logged_in, "Other Story")

    moved = logged_in.put(f"/api/stories/file/{chapter}", json={"dir": target}).json()

    assert moved["dir"] == target
    assert moved["name"] == "first-chapter"
    listing = logged_in.get(f"/api/stories/dir/{target}").json()["files"]
    assert entry_named(listing, "first-chapter")["id"] == moved["id"]


def test_moving_a_chapter_to_an_unknown_story_is_not_found(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.put(f"/api/stories/file/{chapter}", json={"dir": "0" * 16})

    assert response.status_code == 404
    assert logged_in.get(f"/api/stories/file/{chapter}").status_code == 200


def test_a_chapter_with_no_change_asked_for_stays_as_it_is(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.put(f"/api/stories/file/{chapter}", json={})

    assert response.status_code == 200
    assert response.json()["id"] == chapter


# --- deleting --------------------------------------------------------------


def test_deleting_a_chapter_removes_its_file(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    assert logged_in.delete(f"/api/stories/file/{chapter}").status_code == 204

    path = repository / "stories" / "example-story" / "chapters" / "first-chapter.ink"
    assert not path.exists()
    assert logged_in.get(f"/api/stories/file/{chapter}").status_code == 404


def test_deleting_an_unknown_chapter_is_not_found(logged_in: TestClient, repository: Path) -> None:
    assert logged_in.delete(f"/api/stories/file/{'0' * 16}").status_code == 404


def test_deleting_a_story_removes_its_directory(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    assert logged_in.delete(f"/api/stories/dir/{identifier}").status_code == 204

    assert not (repository / "stories" / "example-story").exists()
    assert identifier not in ids(dirs(logged_in))


def test_a_story_holding_a_lorebook_is_not_deleted(logged_in: TestClient, repository: Path) -> None:
    """Refused rather than obeyed: a lorebook is written by hand, not by this API."""
    (repository / "stories" / "example-story" / "lorebook").mkdir()
    identifier = story_id(logged_in, "Example Story")

    response = logged_in.delete(f"/api/stories/dir/{identifier}")
    assert response.status_code == 409
    assert "lorebook" in response.json()["message"]
    assert (repository / "stories" / "example-story").is_dir()


def test_the_409_names_what_is_in_the_way(logged_in: TestClient, repository: Path) -> None:
    (repository / "stories" / "example-story" / "lorebook").mkdir()
    (repository / "stories" / "example-story" / "timeline.yaml").touch()
    identifier = story_id(logged_in, "Example Story")

    body = logged_in.delete(f"/api/stories/dir/{identifier}").json()
    assert body["holds"] == ["lorebook", "timeline.yaml"]


def test_force_true_deletes_a_story_that_would_otherwise_be_refused(
    logged_in: TestClient, repository: Path
) -> None:
    (repository / "stories" / "example-story" / "lorebook").mkdir()
    identifier = story_id(logged_in, "Example Story")

    response = logged_in.delete(f"/api/stories/dir/{identifier}?force=true")
    assert response.status_code == 204
    assert not (repository / "stories" / "example-story").exists()


def test_a_clean_story_still_answers_204_with_no_force(
    logged_in: TestClient, repository: Path
) -> None:
    identifier = story_id(logged_in, "Example Story")
    assert logged_in.delete(f"/api/stories/dir/{identifier}").status_code == 204


def test_force_true_on_a_clean_story_still_204s(
    logged_in: TestClient, repository: Path
) -> None:
    """Force means "do not check", not "found something to override"."""
    identifier = story_id(logged_in, "Example Story")
    assert logged_in.delete(f"/api/stories/dir/{identifier}?force=true").status_code == 204


# --- what a header says ----------------------------------------------------


def test_a_chapter_answers_with_what_its_header_says(
    logged_in: TestClient, repository: Path
) -> None:
    chapters = repository / "stories" / "example-story" / "chapters"
    (chapters / "third.ink").write_text(
        "---\ntitle: The Letter\nstatus: draft\nsummary: She opens it.\n---\nOnce.\n",
        encoding="utf-8",
    )

    identifier = story_id(logged_in, "Example Story")
    listing = logged_in.get(f"/api/stories/dir/{identifier}").json()["files"]
    third = entry_named(listing, "The Letter")["id"]

    assert logged_in.get(f"/api/stories/file/{third}").json() == {
        "id": third,
        "name": "The Letter",
        "status": "draft",
        "summary": "She opens it.",
    }


def test_a_chapter_with_no_header_answers_with_empty_fields(
    logged_in: TestClient, repository: Path
) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    assert logged_in.get(f"/api/stories/file/{chapter}").json() == {
        "id": chapter,
        "name": "first-chapter",
        "status": "",
        "summary": "",
    }


# --- contents --------------------------------------------------------------


def test_a_chapter_is_read_as_plain_text(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.get(f"/api/stories/file/{chapter}/contents")

    assert response.status_code == 200
    assert response.text == "Once."
    assert response.headers["content-type"].startswith("text/plain")


def test_a_chapter_is_written_from_the_request_body(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    text = "Once, on a cold morning — she left.\n"

    response = logged_in.put(
        f"/api/stories/file/{chapter}/contents", content=text.encode(), headers=CONTENT_TYPE
    )

    assert response.status_code == 204
    assert logged_in.get(f"/api/stories/file/{chapter}/contents").text == text
    path = repository / "stories" / "example-story" / "chapters" / "first-chapter.ink"
    assert path.read_text(encoding="utf-8") == text


def test_an_empty_chapter_may_be_written(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.put(
        f"/api/stories/file/{chapter}/contents", content=b"", headers=CONTENT_TYPE
    )

    assert response.status_code == 204
    assert logged_in.get(f"/api/stories/file/{chapter}/contents").text == ""


def test_the_contents_of_an_unknown_chapter_are_not_found(logged_in: TestClient, repository: Path) -> None:
    assert logged_in.get(f"/api/stories/file/{'0' * 16}/contents").status_code == 404
    assert (
        logged_in.put(
            f"/api/stories/file/{'0' * 16}/contents", content=b"Once.", headers=CONTENT_TYPE
        ).status_code
        == 404
    )


def test_a_chapter_that_is_not_utf8_is_refused(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.put(
        f"/api/stories/file/{chapter}/contents", content=b"\xff\xfe", headers=CONTENT_TYPE
    )

    assert response.status_code == 400
    assert "UTF-8" in response.json()["message"]


def test_a_chapter_beyond_the_limit_is_refused(logged_in: TestClient, repository: Path) -> None:

    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.put(
        f"/api/stories/file/{chapter}/contents",
        content=b"x" * (MAX_FILE_BYTES + 1),
        headers=CONTENT_TYPE,
    )

    assert response.status_code == 413
    path = repository / "stories" / "example-story" / "chapters" / "first-chapter.ink"
    assert path.read_text(encoding="utf-8") == "Once."


def test_the_contents_of_a_chapter_leave_out_its_header(
    logged_in: TestClient, repository: Path
) -> None:
    """The editor holds prose, so the header never reaches it and never reaches a model."""
    chapters = repository / "stories" / "example-story" / "chapters"
    (chapters / "first-chapter.ink").write_text(
        "---\ntitle: The Letter\n---\nOnce.\n", encoding="utf-8"
    )
    chapter = chapter_id(logged_in, "Example Story", "The Letter")

    assert logged_in.get(f"/api/stories/file/{chapter}/contents").text == "Once.\n"


def test_saving_a_chapter_keeps_the_header_on_disk(
    logged_in: TestClient, repository: Path
) -> None:
    chapters = repository / "stories" / "example-story" / "chapters"
    path = chapters / "first-chapter.ink"
    path.write_text("---\ntitle: The Letter\n---\nOnce.\n", encoding="utf-8")
    chapter = chapter_id(logged_in, "Example Story", "The Letter")

    response = logged_in.put(
        f"/api/stories/file/{chapter}/contents", content="Twice.\n", headers=CONTENT_TYPE
    )

    assert response.status_code == 204
    assert path.read_text(encoding="utf-8") == "---\ntitle: The Letter\n---\nTwice.\n"


def test_contents_need_a_token(client: TestClient, repository: Path) -> None:
    assert client.get(f"/api/stories/file/{'0' * 16}/contents").status_code == 401
    assert (
        client.put(
            f"/api/stories/file/{'0' * 16}/contents", content=b"Once.", headers=CONTENT_TYPE
        ).status_code
        == 401
    )


# --- who can see what ------------------------------------------------------


@pytest.mark.parametrize("space", ["stories", "notes"])
@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "tree"),
        ("get", f"dir/{'0' * 16}"),
        ("post", "dir"),
        ("put", f"dir/{'0' * 16}"),
        ("delete", f"dir/{'0' * 16}"),
        ("post", "file"),
        ("get", f"file/{'0' * 16}"),
        ("put", f"file/{'0' * 16}"),
        ("delete", f"file/{'0' * 16}"),
        ("get", f"file/{'0' * 16}/contents"),
        ("put", f"file/{'0' * 16}/contents"),
    ],
)
def test_every_route_refuses_a_request_with_no_token(
    client: TestClient, repository: Path, method: str, path: str, space: str
) -> None:
    """Authentication is declared on the router, so this holds for a route added later too."""
    url = f"/api/{space}/{path}"
    response = getattr(client, method)(url)
    assert response.status_code == 401, f"{method.upper()} {url}"


def test_two_accounts_see_the_same_stories(
    logged_in: TestClient, repository: Path, session_factory, settings
) -> None:
    """The stories belong to the repository, not to an account.

    One person writes here and the repository is a single working tree, so there is no
    per-account ownership to enforce: a second account sees the same stories.
    """
    with session_factory() as session:
        session.add(
            User(
                email="second@example.com",
                roles=[],
                password=hash_password("password", settings.bcrypt_rounds),
            )
        )
        session.commit()

    first = logged_in.get("/api/stories/tree").json()
    logged_in.post(
        "/auth", json={"username": "second@example.com", "password": "password"}
    )
    second = logged_in.get("/api/stories/tree").json()

    assert second["user"] == "second@example.com"
    assert second["dirs"] == first["dirs"]


# --- what a writer does in one sitting -------------------------------------


def test_a_story_a_chapter_and_a_save(logged_in: TestClient, data_root: Path) -> None:
    """The whole sequence the editor performs, from an empty repository."""
    story = logged_in.post(
        "/api/stories/dir", json={"name": "Example Story", "summary": "In one line."}
    ).json()
    chapter = logged_in.post(
        "/api/stories/file", json={"name": "First Chapter", "dir": story["id"]}
    ).json()

    assert logged_in.get(f"/api/stories/file/{chapter['id']}/contents").text == ""

    logged_in.put(
        f"/api/stories/file/{chapter['id']}/contents",
        content="Once, on a cold morning.".encode(),
        headers=CONTENT_TYPE,
    )

    tree = logged_in.get("/api/stories/tree").json()
    assert entry_named(tree["dirs"], "Example Story")["id"] == story["id"]
    listing = logged_in.get(f"/api/stories/dir/{story['id']}").json()["files"]
    assert listing == [
        {"id": chapter["id"], "name": "First Chapter", "status": "", "summary": ""}
    ]
    assert (
        logged_in.get(f"/api/stories/file/{chapter['id']}/contents").text
        == "Once, on a cold morning."
    )


# --- the order the chapters are read in --------------------------------------


def test_a_story_lists_its_chapters_in_manifest_order(
    logged_in: TestClient, data_root: Path
) -> None:
    """Not alphabetically: `story.yaml` is what decides, and it reaches the client."""
    make_story(
        data_root,
        "example-story",
        title="Example Story",
        chapters={"alpha.ink": "", "beta.ink": "", "gamma.ink": ""},
        listed=[{"file": "gamma.ink"}, {"file": "alpha.ink"}, {"file": "beta.ink"}],
    )

    assert names(story_files(logged_in, "Example Story")) == ["gamma", "alpha", "beta"]
    assert names(entry_named(dirs(logged_in), "Example Story")["files"]) == [
        "gamma",
        "alpha",
        "beta",
    ]


def test_reordering_answers_the_new_order(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    first = chapter_id(logged_in, "Example Story", "first-chapter")
    second = chapter_id(logged_in, "Example Story", "second-chapter")

    response = logged_in.put(
        f"/api/stories/dir/{identifier}/chapters", json={"order": [second, first]}
    )

    assert response.status_code == 200
    assert names(response.json()["files"]) == ["second-chapter", "first-chapter"]
    assert names(story_files(logged_in, "Example Story")) == [
        "second-chapter",
        "first-chapter",
    ]


def test_reordering_writes_only_the_manifest(
    logged_in: TestClient, repository: Path
) -> None:
    """The chapters' own files are not touched, so nothing about their prose changes."""
    identifier = story_id(logged_in, "Example Story")
    first = chapter_id(logged_in, "Example Story", "first-chapter")
    second = chapter_id(logged_in, "Example Story", "second-chapter")
    chapters_dir = repository / "stories" / "example-story" / "chapters"
    before = {
        path.name: (path.read_text(encoding="utf-8"), path.stat().st_mtime_ns)
        for path in chapters_dir.iterdir()
    }

    logged_in.put(
        f"/api/stories/dir/{identifier}/chapters", json={"order": [second, first]}
    )

    assert manifest(repository, "example-story")["chapters"] == [
        {"file": "second-chapter.ink"},
        {"file": "first-chapter.ink"},
    ]
    assert {
        path.name: (path.read_text(encoding="utf-8"), path.stat().st_mtime_ns)
        for path in chapters_dir.iterdir()
    } == before


def test_reordering_is_idempotent(logged_in: TestClient, repository: Path) -> None:
    """A retry after a lost response must not shuffle anything."""
    identifier = story_id(logged_in, "Example Story")
    order = [
        chapter_id(logged_in, "Example Story", "second-chapter"),
        chapter_id(logged_in, "Example Story", "first-chapter"),
    ]
    url = f"/api/stories/dir/{identifier}/chapters"

    once = logged_in.put(url, json={"order": order}).json()
    twice = logged_in.put(url, json={"order": order}).json()

    assert once == twice


def test_reordering_refuses_a_chapter_of_another_story(
    logged_in: TestClient, repository: Path
) -> None:
    identifier = story_id(logged_in, "Example Story")
    elsewhere = logged_in.post(
        "/api/stories/file",
        json={"name": "Loose", "dir": story_id(logged_in, "Other Story")},
    ).json()["id"]

    response = logged_in.put(
        f"/api/stories/dir/{identifier}/chapters", json={"order": [elsewhere]}
    )

    assert response.status_code == 422
    assert elsewhere in response.json()["message"]


def test_reordering_refuses_a_repeated_chapter(
    logged_in: TestClient, repository: Path
) -> None:
    identifier = story_id(logged_in, "Example Story")
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")

    response = logged_in.put(
        f"/api/stories/dir/{identifier}/chapters", json={"order": [chapter, chapter]}
    )

    assert response.status_code == 422
    assert "twice" in response.json()["message"]


def test_reordering_an_unknown_story_is_not_found(
    logged_in: TestClient, repository: Path
) -> None:
    response = logged_in.put(
        f"/api/stories/dir/{'0' * 16}/chapters", json={"order": []}
    )
    assert response.status_code == 404


def test_reordering_needs_a_token(client: TestClient, repository: Path) -> None:
    response = client.put(f"/api/stories/dir/{'0' * 16}/chapters", json={"order": []})
    assert response.status_code == 401


def test_the_notes_root_has_no_order_to_set(
    logged_in: TestClient, files_root: Path
) -> None:
    """A folder there records no order, so there is no such path in that space at all —
    which is a 404 rather than the 405 an existing path with another method would give."""
    folder = logged_in.post("/api/notes/dir", json={"name": "Research"}).json()["id"]
    response = logged_in.put(f"/api/notes/dir/{folder}/chapters", json={"order": []})
    assert response.status_code == 404


# --- setting a status -------------------------------------------------------


def test_a_status_is_set_through_the_header(
    logged_in: TestClient, repository: Path
) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")

    response = logged_in.put(
        f"/api/stories/file/{chapter}", json={"status": "draft"}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "draft"
    assert response.json()["id"] == chapter, "a status change moves no file"
    path = repository / "stories" / "example-story" / "chapters" / "first-chapter.ink"
    assert path.read_text(encoding="utf-8") == "---\nstatus: draft\n---\nOnce."


def test_a_status_reaches_the_listing(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    logged_in.put(f"/api/stories/file/{chapter}", json={"status": "draft"})

    listed = entry_named(story_files(logged_in, "Example Story"), "first-chapter")
    assert listed["status"] == "draft"
    intree = entry_named(
        entry_named(dirs(logged_in), "Example Story")["files"], "first-chapter"
    )
    assert intree["status"] == "draft"


def test_an_empty_status_clears_it(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    logged_in.put(f"/api/stories/file/{chapter}", json={"status": "draft"})

    response = logged_in.put(f"/api/stories/file/{chapter}", json={"status": ""})

    assert response.json()["status"] == ""
    path = repository / "stories" / "example-story" / "chapters" / "first-chapter.ink"
    assert path.read_text(encoding="utf-8") == "Once."


def test_a_summary_is_set_through_the_header(
    logged_in: TestClient, repository: Path
) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")

    response = logged_in.put(
        f"/api/stories/file/{chapter}", json={"summary": "She opens it."}
    )

    assert response.json()["summary"] == "She opens it."
    assert logged_in.get(f"/api/stories/file/{chapter}").json()["summary"] == (
        "She opens it."
    )


def test_a_status_does_not_disturb_the_prose(
    logged_in: TestClient, repository: Path
) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    logged_in.put(
        f"/api/stories/file/{chapter}/contents",
        content="Once, on a cold morning.".encode(),
        headers=CONTENT_TYPE,
    )

    logged_in.put(f"/api/stories/file/{chapter}", json={"status": "draft"})

    assert logged_in.get(f"/api/stories/file/{chapter}/contents").text == (
        "Once, on a cold morning."
    )


def test_a_status_has_a_limit(logged_in: TestClient, repository: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.put(
        f"/api/stories/file/{chapter}", json={"status": "x" * 65}
    )

    assert response.status_code == 400
    assert "64" in response.json()["message"]


def test_a_status_is_set_on_a_note_too(logged_in: TestClient, files_root: Path) -> None:
    """Both roots hold `.ink` files, so a note has a status in the same way."""
    note = logged_in.post("/api/notes/file", json={"name": "Scratch"}).json()["id"]

    response = logged_in.put(f"/api/notes/file/{note}", json={"status": "draft"})

    assert response.json()["status"] == "draft"
    assert logged_in.get(f"/api/notes/file/{note}").json()["status"] == "draft"
