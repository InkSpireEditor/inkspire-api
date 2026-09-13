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

from conftest import EMAIL, make_story

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


def dirs(client: TestClient) -> dict:
    return client.get("/api/stories/tree").json()["dirs"]


def story_id(client: TestClient, name: str) -> str:
    """The id the tree gives the story called `name`."""
    for identifier, story in dirs(client).items():
        if story["name"] == name:
            return identifier
    raise AssertionError(f'No story named "{name}" in the tree.')


def chapter_id(client: TestClient, story: str, chapter: str) -> str:
    """The id the tree gives the chapter called `chapter`, in the story called `story`."""
    response = client.get(f"/api/stories/dir/{story_id(client, story)}")
    for identifier, file in response.json()["files"].items():
        if file["name"] == chapter:
            return identifier
    raise AssertionError(f'No chapter named "{chapter}" in "{story}".')


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
    assert sorted(story["name"] for story in body["dirs"].values()) == [
        "Example Story",
        "Other Story",
    ]
    assert body["dirs"][story_id(logged_in, "Example Story")] == {
        "name": "Example Story",
        "summary": "In one line.",
    }


def test_the_tree_has_no_loose_files(logged_in: TestClient, repository: Path) -> None:
    """Every chapter belongs to a story, so nothing sits at the root of this space.

    A file that belongs to no story lives in the other root. See `test_notes_routes.py`.
    """
    assert logged_in.get("/api/stories/tree").json()["files"] == {}


def test_an_empty_repository_is_an_empty_tree(logged_in: TestClient, data_root: Path) -> None:
    assert logged_in.get("/api/stories/tree").json() == {"user": EMAIL, "files": {}, "dirs": {}}


def test_a_repository_that_is_not_there_is_an_empty_tree(logged_in: TestClient) -> None:
    """An installation that has not cloned the stories yet still serves the app."""
    assert logged_in.get("/api/stories/tree").json()["dirs"] == {}


# --- one story -------------------------------------------------------------


def test_a_story_lists_its_chapters(logged_in: TestClient, repository: Path) -> None:
    identifier = story_id(logged_in, "Example Story")
    body = logged_in.get(f"/api/stories/dir/{identifier}").json()

    assert body["id"] == identifier
    assert body["name"] == "Example Story"
    assert body["summary"] == "In one line."
    assert sorted(file["name"] for file in body["files"].values()) == [
        "first-chapter",
        "second-chapter",
    ]


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
    assert created["id"] in dirs(logged_in)


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
    assert listing[created["id"]] == {"name": "Third Chapter"}


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
    assert listing[moved["id"]] == {"name": "first-chapter"}


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
    assert identifier not in dirs(logged_in)


def test_a_story_holding_a_lorebook_is_not_deleted(logged_in: TestClient, repository: Path) -> None:
    """Refused rather than obeyed: a lorebook is written by hand, not by this API."""
    (repository / "stories" / "example-story" / "lorebook").mkdir()
    identifier = story_id(logged_in, "Example Story")

    response = logged_in.delete(f"/api/stories/dir/{identifier}")
    assert response.status_code == 409
    assert "lorebook" in response.json()["message"]
    assert (repository / "stories" / "example-story").is_dir()


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
    third = next(k for k, v in listing.items() if v["name"] == "The Letter")

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
    assert tree["dirs"][story["id"]]["name"] == "Example Story"
    listing = logged_in.get(f"/api/stories/dir/{story['id']}").json()["files"]
    assert listing == {chapter["id"]: {"name": "First Chapter"}}
    assert (
        logged_in.get(f"/api/stories/file/{chapter['id']}/contents").text
        == "Once, on a cold morning."
    )
