# -*- coding: utf-8 -*-
"""The routes over the second root, which is what fixes the two broken buttons.

A file may sit at the root here and a directory is not a story, so these are the two
places the shape differs from `/api/stories`: `dir: null` on create means the root, and
`dir` left out of an update means "leave it where it is".
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.conftest import EMAIL, entry_named, make_folder, make_note

CONTENT_TYPE = {"Content-Type": "text/plain"}


@pytest.fixture
def workspace(files_root: Path) -> Path:
    """A file at the root, and a folder holding one."""
    make_note(files_root, "scratch.ink", "A list.\n")
    folder = make_folder(files_root, "research", context="Background reading.")
    make_note(folder, "worldbuilding.ink", "===== ink:meta\ntitle: Worldbuilding\n===== ink:body\nOnce.\n")
    return files_root


def tree(client: TestClient) -> dict:
    response = client.get("/api/notes/tree")
    assert response.status_code == 200, response.text
    return response.json()


def folder_id(client: TestClient, name: str) -> str:
    return entry_named(tree(client)["dirs"], name)["id"]


def note_id(client: TestClient, name: str, folder: str | None = None) -> str:
    listing = (
        tree(client)["files"]
        if folder is None
        else client.get(f"/api/notes/dir/{folder}").json()["files"]
    )
    return entry_named(listing, name)["id"]


def names(listing: list[dict]) -> list[str]:
    """The names in a listing, in the order it gives them."""
    return [entry["name"] for entry in listing]


# --- the tree ---------------------------------------------------------------


def test_the_tree_holds_the_folders_and_the_files_at_the_root(
    logged_in: TestClient, workspace: Path
) -> None:
    answered = tree(logged_in)

    assert answered["user"] == EMAIL
    assert names(answered["files"]) == ["scratch"]
    assert len(answered["dirs"]) == 1
    research = answered["dirs"][0]
    assert research.pop("modified")
    for note in research["files"]:
        assert note.pop("modified")
    assert research == {
        "id": folder_id(logged_in, "research"),
        "name": "research",
        "summary": "Background reading.",
        "files": [
            {
                "id": note_id(logged_in, "Worldbuilding",
                              folder_id(logged_in, "research")),
                "name": "Worldbuilding",
                "status": "",
                "tags": [],
            }
        ],
    }


def test_a_file_in_a_folder_is_not_at_the_root(
    logged_in: TestClient, workspace: Path
) -> None:
    assert "Worldbuilding" not in names(tree(logged_in)["files"])


def test_a_root_that_is_not_there_is_an_empty_tree(
    logged_in: TestClient, settings
) -> None:
    """It is made when something is first written, so nothing has to exist yet."""
    assert not settings.files_root.exists()
    assert tree(logged_in) == {"user": EMAIL, "files": [], "dirs": []}


def test_a_folder_answers_with_its_context_and_its_files(
    logged_in: TestClient, workspace: Path
) -> None:
    identifier = folder_id(logged_in, "research")
    answered = logged_in.get(f"/api/notes/dir/{identifier}").json()

    assert answered["name"] == "research"
    assert answered["summary"] == "Background reading."
    assert names(answered["files"]) == ["Worldbuilding"]


def test_an_unknown_folder_is_not_found(logged_in: TestClient, workspace: Path) -> None:
    assert logged_in.get(f"/api/notes/dir/{'0' * 16}").status_code == 404


# --- the two buttons that were broken ---------------------------------------


def test_a_file_can_be_created_at_the_root(
    logged_in: TestClient, files_root: Path
) -> None:
    """`POST /api/stories/file` with `dir: null` is a 422. Here it is the point."""
    response = logged_in.post("/api/notes/file", json={"name": "Scratch Pad", "dir": None})

    assert response.status_code == 201
    assert response.json()["name"] == "Scratch Pad"
    assert response.json()["dir"] is None
    assert (files_root / "scratch-pad.ink").read_text(encoding="utf-8") == (
        "===== ink:meta\ntitle: Scratch Pad\n===== ink:body\n"
    )


def test_a_file_can_be_created_with_no_dir_field_at_all(
    logged_in: TestClient, files_root: Path
) -> None:
    response = logged_in.post("/api/notes/file", json={"name": "Scratch Pad"})
    assert response.status_code == 201
    assert response.json()["dir"] is None


def test_a_directory_can_be_created_that_is_not_a_story(
    logged_in: TestClient, files_root: Path
) -> None:
    """`POST /api/stories/dir` can only make a story, since it writes a `story.yaml`."""
    response = logged_in.post("/api/notes/dir", json={"name": "Research"})

    assert response.status_code == 201
    assert response.json()["name"] == "Research"
    assert response.json()["summary"] == ""
    assert (files_root / "research").is_dir()
    assert not (files_root / "research" / "story.yaml").exists()


def test_a_created_folder_needing_nothing_recorded_is_a_bare_directory(
    logged_in: TestClient, files_root: Path
) -> None:
    logged_in.post("/api/notes/dir", json={"name": "research"})
    assert list((files_root / "research").iterdir()) == []


# --- creating ---------------------------------------------------------------


def test_a_created_folder_keeps_a_name_its_slug_cannot_say(
    logged_in: TestClient, files_root: Path
) -> None:
    logged_in.post("/api/notes/dir", json={"name": "Research Notes"})

    document = yaml.safe_load(
        (files_root / "research-notes" / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert document == {"title": "Research Notes"}


def test_a_created_folder_keeps_its_context(
    logged_in: TestClient, files_root: Path
) -> None:
    response = logged_in.post(
        "/api/notes/dir", json={"name": "Research", "summary": "Background reading."}
    )

    assert response.json()["summary"] == "Background reading."
    document = yaml.safe_load(
        (files_root / "research" / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert document == {"title": "Research", "context": "Background reading."}


def test_a_file_is_created_in_the_folder_it_names(
    logged_in: TestClient, workspace: Path
) -> None:
    identifier = folder_id(logged_in, "research")
    response = logged_in.post(
        "/api/notes/file", json={"name": "Names", "dir": identifier}
    )

    assert response.status_code == 201
    assert response.json()["dir"] == identifier
    assert (workspace / "research" / "names.ink").is_file()


def test_a_file_in_an_unknown_folder_is_not_found(
    logged_in: TestClient, files_root: Path
) -> None:
    response = logged_in.post("/api/notes/file", json={"name": "Names", "dir": "0" * 16})
    assert response.status_code == 404


def test_a_folder_needs_a_name(logged_in: TestClient, files_root: Path) -> None:
    assert logged_in.post("/api/notes/dir", json={"name": "   "}).status_code == 400
    assert logged_in.post("/api/notes/dir", json={}).status_code == 400


def test_a_context_has_a_limit(logged_in: TestClient, files_root: Path) -> None:
    response = logged_in.post(
        "/api/notes/dir", json={"name": "Research", "summary": "x" * 2001}
    )
    assert response.status_code == 400
    assert "2000" in response.json()["message"]


# --- what a header says -----------------------------------------------------


def test_a_file_answers_with_what_its_header_says(
    logged_in: TestClient, files_root: Path
) -> None:
    make_note(
        files_root,
        "scratch.ink",
        "===== ink:meta\ntitle: Scratch Pad\nstatus: draft\nsummary: A list.\n===== ink:body\nOnce.\n",
    )
    identifier = note_id(logged_in, "Scratch Pad")

    body = logged_in.get(f"/api/notes/file/{identifier}").json()
    assert body.pop("modified")
    assert body == {
        "id": identifier,
        "name": "Scratch Pad",
        "status": "draft",
        "tags": [],
        "summary": "A list.",
    }


# --- renaming and moving ----------------------------------------------------


def test_renaming_a_folder_keeps_its_id(logged_in: TestClient, workspace: Path) -> None:
    identifier = folder_id(logged_in, "research")
    response = logged_in.put(
        f"/api/notes/dir/{identifier}", json={"name": "Research Notes"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "id": identifier,
        "name": "Research Notes",
        "summary": "Background reading.",
    }


def test_rewriting_only_the_context_keeps_the_name(
    logged_in: TestClient, workspace: Path
) -> None:
    identifier = folder_id(logged_in, "research")
    response = logged_in.put(f"/api/notes/dir/{identifier}", json={"summary": "Later."})

    assert response.json()["name"] == "research"
    assert response.json()["summary"] == "Later."


def test_renaming_a_file_renames_it_and_changes_its_id(
    logged_in: TestClient, workspace: Path
) -> None:
    identifier = note_id(logged_in, "scratch")
    response = logged_in.put(
        f"/api/notes/file/{identifier}", json={"name": "Scratch Pad"}
    )

    assert response.status_code == 200
    assert response.json()["id"] != identifier
    assert (workspace / "scratch-pad.ink").read_text(encoding="utf-8") == (
        "===== ink:meta\ntitle: Scratch Pad\n===== ink:body\nA list.\n"
    )


def test_renaming_a_file_leaves_it_where_it_is(
    logged_in: TestClient, workspace: Path
) -> None:
    """`dir` is not in the request, so the folder is not what is being changed."""
    folder = folder_id(logged_in, "research")
    identifier = note_id(logged_in, "Worldbuilding", folder)

    response = logged_in.put(f"/api/notes/file/{identifier}", json={"name": "Names"})

    assert response.json()["dir"] == folder
    assert (workspace / "research" / "names.ink").is_file()


def test_moving_a_file_into_a_folder(logged_in: TestClient, workspace: Path) -> None:
    folder = folder_id(logged_in, "research")
    identifier = note_id(logged_in, "scratch")

    response = logged_in.put(f"/api/notes/file/{identifier}", json={"dir": folder})

    assert response.json()["dir"] == folder
    assert (workspace / "research" / "scratch.ink").is_file()
    assert not (workspace / "scratch.ink").exists()


def test_moving_a_file_back_out_to_the_root(
    logged_in: TestClient, workspace: Path
) -> None:
    """An explicit `null` is the root, which is what tells it from an absent field."""
    folder = folder_id(logged_in, "research")
    identifier = note_id(logged_in, "Worldbuilding", folder)

    response = logged_in.put(f"/api/notes/file/{identifier}", json={"dir": None})

    assert response.json()["dir"] is None
    assert (workspace / "worldbuilding.ink").is_file()
    assert not (workspace / "research" / "worldbuilding.ink").exists()


def test_moving_a_file_to_an_unknown_folder_is_not_found(
    logged_in: TestClient, workspace: Path
) -> None:
    identifier = note_id(logged_in, "scratch")
    response = logged_in.put(f"/api/notes/file/{identifier}", json={"dir": "0" * 16})
    assert response.status_code == 404


# --- deleting ---------------------------------------------------------------


def test_deleting_a_file_removes_it(logged_in: TestClient, workspace: Path) -> None:
    identifier = note_id(logged_in, "scratch")
    response = logged_in.delete(f"/api/notes/file/{identifier}")

    assert response.status_code == 204
    assert not (workspace / "scratch.ink").exists()


def test_deleting_a_folder_removes_its_directory(
    logged_in: TestClient, workspace: Path
) -> None:
    identifier = folder_id(logged_in, "research")
    response = logged_in.delete(f"/api/notes/dir/{identifier}")

    assert response.status_code == 204
    assert not (workspace / "research").exists()


def test_a_folder_holding_anything_else_is_not_deleted(
    logged_in: TestClient, workspace: Path
) -> None:
    (workspace / "research" / "sources.md").write_text("A list.\n", encoding="utf-8")
    identifier = folder_id(logged_in, "research")

    response = logged_in.delete(f"/api/notes/dir/{identifier}")

    assert response.status_code == 409
    assert "sources.md" in response.json()["message"]
    assert (workspace / "research").is_dir()


# --- contents ---------------------------------------------------------------


def test_a_file_is_read_as_plain_text(logged_in: TestClient, workspace: Path) -> None:
    identifier = note_id(logged_in, "scratch")
    response = logged_in.get(f"/api/notes/file/{identifier}/contents")

    assert response.status_code == 200
    assert response.text == "A list.\n"
    assert response.headers["content-type"].startswith("text/plain")


def test_the_contents_of_a_file_leave_out_its_header(
    logged_in: TestClient, workspace: Path
) -> None:
    folder = folder_id(logged_in, "research")
    identifier = note_id(logged_in, "Worldbuilding", folder)
    assert logged_in.get(f"/api/notes/file/{identifier}/contents").text == "Once.\n"


def test_saving_a_file_keeps_its_header(logged_in: TestClient, workspace: Path) -> None:
    folder = folder_id(logged_in, "research")
    identifier = note_id(logged_in, "Worldbuilding", folder)

    response = logged_in.put(f"/api/notes/file/{identifier}/document", json={"body": "Twice.\n"})

    assert response.status_code == 200
    path = workspace / "research" / "worldbuilding.ink"
    assert path.read_text(encoding="utf-8") == (
        "===== ink:meta\ntitle: Worldbuilding\n===== ink:body\nTwice.\n"
    )


def test_a_file_that_is_not_json_is_refused(
    logged_in: TestClient, workspace: Path
) -> None:
    """A save sends JSON, so bytes that are not text fail to parse before reaching the
    file. Same as the stories root, which is the point: one route, two roots."""
    identifier = note_id(logged_in, "scratch")
    before = logged_in.get(f"/api/notes/file/{identifier}/contents").text
    response = logged_in.put(
        f"/api/notes/file/{identifier}/document", content=b"\xff\xfe", headers=CONTENT_TYPE
    )

    assert response.status_code == 400
    assert logged_in.get(f"/api/notes/file/{identifier}/contents").text == before


def test_the_contents_of_an_unknown_file_are_not_found(
    logged_in: TestClient, workspace: Path
) -> None:
    assert logged_in.get(f"/api/notes/file/{'0' * 16}/contents").status_code == 404


# --- the two spaces do not reach into each other ----------------------------


def test_a_story_id_is_not_found_here(
    logged_in: TestClient, workspace: Path, data_root: Path
) -> None:
    """Ids carry their space, so one from the other root names nothing here."""
    story = logged_in.post("/api/stories/dir", json={"name": "Example Story"}).json()

    assert logged_in.get(f"/api/notes/dir/{story['id']}").status_code == 404


def test_a_folder_id_is_not_found_in_the_stories(
    logged_in: TestClient, workspace: Path, data_root: Path
) -> None:
    identifier = folder_id(logged_in, "research")
    assert logged_in.get(f"/api/stories/dir/{identifier}").status_code == 404


def test_nothing_written_here_reaches_the_story_repository(
    logged_in: TestClient, workspace: Path, data_root: Path
) -> None:
    """The notes are not novel content, and the repository is what gets committed."""
    logged_in.post("/api/notes/dir", json={"name": "Research"})
    logged_in.post("/api/notes/file", json={"name": "Scratch Pad"})

    assert list((data_root / "stories").iterdir()) == []


# --- what a writer does in one sitting --------------------------------------


def test_a_folder_a_file_and_a_save(logged_in: TestClient, files_root: Path) -> None:
    folder = logged_in.post(
        "/api/notes/dir", json={"name": "Research", "summary": "Background reading."}
    ).json()
    note = logged_in.post(
        "/api/notes/file", json={"name": "Names", "dir": folder["id"]}
    ).json()

    logged_in.put(
        f"/api/notes/file/{note['id']}/document",
        json={"body": "Jane Doe. John Smith.\n"},
    )

    assert logged_in.get(f"/api/notes/file/{note['id']}/contents").text == (
        "Jane Doe. John Smith.\n"
    )
    listing = logged_in.get(f"/api/notes/dir/{folder['id']}").json()
    assert listing["summary"] == "Background reading."
    assert names(listing["files"]) == ["Names"]


def test_the_tree_carries_every_folder_s_files_in_one_request(
    logged_in: TestClient, files_root: Path
) -> None:
    """As the stories tree does: no request per folder to find out what is in it."""
    first = make_folder(files_root, "research")
    make_note(first, "worldbuilding.ink")
    second = make_folder(files_root, "admin")
    make_note(second, "todo.ink")

    answered = tree(logged_in)

    assert names(answered["dirs"]) == ["admin", "research"]
    assert names(entry_named(answered["dirs"], "research")["files"]) == [
        "worldbuilding"
    ]
    assert names(entry_named(answered["dirs"], "admin")["files"]) == ["todo"]


def test_both_lists_are_in_name_order(
    logged_in: TestClient, files_root: Path
) -> None:
    """This root records no order of its own, so the name is what settles one."""
    make_note(files_root, "zeta.ink")
    make_note(files_root, "alpha.ink")
    folder = make_folder(files_root, "research")
    make_note(folder, "zulu.ink")
    make_note(folder, "bravo.ink")

    answered = tree(logged_in)

    assert names(answered["files"]) == ["alpha", "zeta"]
    assert names(entry_named(answered["dirs"], "research")["files"]) == [
        "bravo",
        "zulu",
    ]


def test_a_file_at_the_root_carries_its_status(
    logged_in: TestClient, files_root: Path
) -> None:
    make_note(files_root, "scratch.ink", "===== ink:meta\nstatus: draft\n===== ink:body\nA list.\n")
    assert entry_named(tree(logged_in)["files"], "scratch")["status"] == "draft"


# --- when a file last changed ------------------------------------------------


def set_time(path: Path, seconds: int) -> None:
    """Sets a file's modification time, so a test does not depend on the clock."""
    os.utime(path, (seconds, seconds))


PAST = 1_700_000_000
LATER = 1_760_000_000


def test_a_note_in_a_folder_and_one_at_the_root_answer_when_they_last_changed(
    logged_in: TestClient, files_root: Path
) -> None:
    make_note(files_root, "scratch.ink", "A list.\n")
    folder = make_folder(files_root, "research")
    make_note(folder, "worldbuilding.ink", "Once.\n")
    set_time(files_root / "scratch.ink", PAST)
    set_time(folder / "worldbuilding.ink", LATER)

    answered = tree(logged_in)
    assert entry_named(answered["files"], "scratch")["modified"] == (
        "2023-11-14T22:13:20Z"
    )
    assert entry_named(entry_named(answered["dirs"], "research")["files"],
                       "worldbuilding")["modified"] == (
        "2025-10-09T08:53:20Z"
    )


def test_a_folder_answers_the_newest_of_its_manifest_and_its_files(
    logged_in: TestClient, files_root: Path
) -> None:
    folder = make_folder(files_root, "research", context="Background reading.")
    make_note(folder, "worldbuilding.ink", "Once.\n")
    set_time(folder / "worldbuilding.ink", PAST)
    set_time(folder / "manifest.yaml", LATER)

    identifier = folder_id(logged_in, "research")
    answered = logged_in.get(f"/api/notes/dir/{identifier}").json()
    assert answered["modified"] == "2025-10-09T08:53:20Z"


def test_a_folder_with_no_manifest_and_no_files_answers_null(
    logged_in: TestClient, files_root: Path
) -> None:
    make_folder(files_root, "research")

    identifier = folder_id(logged_in, "research")
    answered = logged_in.get(f"/api/notes/dir/{identifier}").json()
    assert answered["modified"] is None


def test_a_folder_with_a_manifest_only_answers_its_manifest_s_time(
    logged_in: TestClient, files_root: Path
) -> None:
    folder = make_folder(files_root, "research", context="Background reading.")
    set_time(folder / "manifest.yaml", PAST)

    identifier = folder_id(logged_in, "research")
    answered = logged_in.get(f"/api/notes/dir/{identifier}").json()
    assert answered["modified"] == "2023-11-14T22:13:20Z"


def test_the_note_route_answers_when_a_file_last_changed(
    logged_in: TestClient, files_root: Path
) -> None:
    make_note(files_root, "scratch.ink", "A list.\n")
    set_time(files_root / "scratch.ink", PAST)
    identifier = note_id(logged_in, "scratch")

    body = logged_in.get(f"/api/notes/file/{identifier}").json()
    assert body["modified"] == "2023-11-14T22:13:20Z"


def test_saving_a_note_moves_its_time(
    logged_in: TestClient, files_root: Path
) -> None:
    """A save restamps the held scan and keeps it, so a time the scan carried would
    stay what it was when the scan was built. Nothing touches the file between the
    first request, which builds the scan, and the save."""
    make_note(files_root, "scratch.ink", "A list.\n")
    set_time(files_root / "scratch.ink", PAST)
    identifier = note_id(logged_in, "scratch")

    before = logged_in.get(f"/api/notes/file/{identifier}").json()["modified"]
    assert before == "2023-11-14T22:13:20Z"

    logged_in.put(f"/api/notes/file/{identifier}/document", json={"body": "Twice.\n"})

    after = logged_in.get(f"/api/notes/file/{identifier}").json()["modified"]
    written = datetime.fromisoformat(after.replace("Z", "+00:00"))
    assert written > datetime.fromisoformat(before.replace("Z", "+00:00"))
    assert datetime.now(timezone.utc) - written < timedelta(minutes=1)


# --- tags ------------------------------------------------------------------


def test_tags_are_set_on_a_note_in_a_folder_and_one_at_the_root(
    logged_in: TestClient, workspace: Path
) -> None:
    in_folder = note_id(logged_in, "Worldbuilding", folder_id(logged_in, "research"))
    at_root = note_id(logged_in, "scratch")

    first = logged_in.put(f"/api/notes/file/{in_folder}", json={"tags": [" Perso"]})
    second = logged_in.put(f"/api/notes/file/{at_root}", json={"tags": ["priority", "perso"]})

    assert first.json()["tags"] == ["perso"]
    assert second.json()["tags"] == ["priority", "perso"]
    assert logged_in.get(f"/api/notes/file/{in_folder}").json()["tags"] == ["perso"]


def test_tags_reach_the_tree_and_the_folders_listing(
    logged_in: TestClient, workspace: Path
) -> None:
    folder = folder_id(logged_in, "research")
    in_folder = note_id(logged_in, "Worldbuilding", folder)
    logged_in.put(f"/api/notes/file/{in_folder}", json={"tags": ["perso"]})

    answered = tree(logged_in)
    research = entry_named(answered["dirs"], "research")
    assert entry_named(research["files"], "Worldbuilding")["tags"] == ["perso"]
    assert entry_named(answered["files"], "scratch")["tags"] == []
    listing = logged_in.get(f"/api/notes/dir/{folder}").json()["files"]
    assert entry_named(listing, "Worldbuilding")["tags"] == ["perso"]


def test_a_note_written_by_hand_with_tags_is_read(
    logged_in: TestClient, files_root: Path
) -> None:
    make_note(
        files_root,
        "tagged.ink",
        "===== ink:meta\ntags: [Perso, priority]\n===== ink:body\nOnce.\n",
    )

    listed = entry_named(tree(logged_in)["files"], "tagged")

    assert listed["tags"] == ["perso", "priority"]


def test_a_bad_note_tag_is_a_400_and_clearing_removes_the_key(
    logged_in: TestClient, files_root: Path
) -> None:
    make_note(
        files_root, "tagged.ink", "===== ink:meta\ntags: [perso]\n===== ink:body\nOnce.\n"
    )
    identifier = note_id(logged_in, "tagged")

    bad = logged_in.put(f"/api/notes/file/{identifier}", json={"tags": ["no way"]})
    cleared = logged_in.put(f"/api/notes/file/{identifier}", json={"tags": []})

    assert bad.status_code == 400
    assert cleared.json()["tags"] == []
    assert (files_root / "tagged.ink").read_text(encoding="utf-8") == "Once.\n"
