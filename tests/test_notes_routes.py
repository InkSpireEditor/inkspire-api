# -*- coding: utf-8 -*-
"""The routes over the second root, which is what fixes the two broken buttons.

A file may sit at the root here and a directory is not a story, so these are the two
places the shape differs from `/api/stories`: `dir: null` on create means the root, and
`dir` left out of an update means "leave it where it is".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from conftest import EMAIL, make_folder, make_note

CONTENT_TYPE = {"Content-Type": "text/plain"}


@pytest.fixture
def workspace(files_root: Path) -> Path:
    """A file at the root, and a folder holding one."""
    make_note(files_root, "scratch.ink", "A list.\n")
    folder = make_folder(files_root, "research", context="Background reading.")
    make_note(folder, "worldbuilding.ink", "---\ntitle: Worldbuilding\n---\nOnce.\n")
    return files_root


def tree(client: TestClient) -> dict:
    response = client.get("/api/notes/tree")
    assert response.status_code == 200, response.text
    return response.json()


def folder_id(client: TestClient, name: str) -> str:
    for identifier, folder in tree(client)["dirs"].items():
        if folder["name"] == name:
            return identifier
    raise AssertionError(f'No folder named "{name}".')


def note_id(client: TestClient, name: str, folder: str | None = None) -> str:
    listing = (
        tree(client)["files"]
        if folder is None
        else client.get(f"/api/notes/dir/{folder}").json()["files"]
    )
    for identifier, note in listing.items():
        if note["name"] == name:
            return identifier
    raise AssertionError(f'No file named "{name}".')


# --- the tree ---------------------------------------------------------------


def test_the_tree_holds_the_folders_and_the_files_at_the_root(
    logged_in: TestClient, workspace: Path
) -> None:
    answered = tree(logged_in)

    assert answered["user"] == EMAIL
    assert [file["name"] for file in answered["files"].values()] == ["scratch"]
    assert list(answered["dirs"].values()) == [
        {"name": "research", "summary": "Background reading."}
    ]


def test_a_file_in_a_folder_is_not_at_the_root(
    logged_in: TestClient, workspace: Path
) -> None:
    assert "Worldbuilding" not in [f["name"] for f in tree(logged_in)["files"].values()]


def test_a_root_that_is_not_there_is_an_empty_tree(
    logged_in: TestClient, settings
) -> None:
    """It is made when something is first written, so nothing has to exist yet."""
    assert not settings.files_root.exists()
    assert tree(logged_in) == {"user": EMAIL, "files": {}, "dirs": {}}


def test_a_folder_answers_with_its_context_and_its_files(
    logged_in: TestClient, workspace: Path
) -> None:
    identifier = folder_id(logged_in, "research")
    answered = logged_in.get(f"/api/notes/dir/{identifier}").json()

    assert answered["name"] == "research"
    assert answered["summary"] == "Background reading."
    assert [file["name"] for file in answered["files"].values()] == ["Worldbuilding"]


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
        "---\ntitle: Scratch Pad\n---\n"
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
        "---\ntitle: Scratch Pad\nstatus: draft\nsummary: A list.\n---\nOnce.\n",
    )
    identifier = note_id(logged_in, "Scratch Pad")

    assert logged_in.get(f"/api/notes/file/{identifier}").json() == {
        "id": identifier,
        "name": "Scratch Pad",
        "status": "draft",
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
        "---\ntitle: Scratch Pad\n---\nA list.\n"
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

    response = logged_in.put(
        f"/api/notes/file/{identifier}/contents", content="Twice.\n", headers=CONTENT_TYPE
    )

    assert response.status_code == 204
    path = workspace / "research" / "worldbuilding.ink"
    assert path.read_text(encoding="utf-8") == (
        "---\ntitle: Worldbuilding\n---\nTwice.\n"
    )


def test_a_file_that_is_not_utf8_is_refused(
    logged_in: TestClient, workspace: Path
) -> None:
    identifier = note_id(logged_in, "scratch")
    response = logged_in.put(
        f"/api/notes/file/{identifier}/contents", content=b"\xff\xfe", headers=CONTENT_TYPE
    )

    assert response.status_code == 400
    assert "UTF-8" in response.json()["message"]


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
        f"/api/notes/file/{note['id']}/contents",
        content="Kanamori. Mizuki. Sae.\n",
        headers=CONTENT_TYPE,
    )

    assert logged_in.get(f"/api/notes/file/{note['id']}/contents").text == (
        "Kanamori. Mizuki. Sae.\n"
    )
    listing = logged_in.get(f"/api/notes/dir/{folder['id']}").json()
    assert listing["summary"] == "Background reading."
    assert [file["name"] for file in listing["files"].values()] == ["Names"]
