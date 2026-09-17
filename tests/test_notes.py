# -*- coding: utf-8 -*-
"""The second root: what counts as a folder, what counts as a file, and the ids.

These tests go straight to the scanner, with no HTTP and no account. The routes over it
are in `test_notes_routes.py`, and the header each file carries is `test_ink.py`.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

import pytest
import yaml

from inkspire_api.fs import Conflict, NotFound, StorageError, derive_id
from inkspire_api.notes import SPACE, NotesScanner

from tests.conftest import make_folder, make_note


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "files"


@pytest.fixture
def notes(root: Path) -> NotesScanner:
    root.mkdir()
    return NotesScanner(root)


def only_folder(scanner: NotesScanner):
    folders = list(scanner.tree().folders.values())
    assert len(folders) == 1, folders
    return folders[0]


# --- what is a folder, what is a file ---------------------------------------


def test_a_root_with_nothing_in_it_is_an_empty_tree(notes: NotesScanner) -> None:
    assert notes.tree().folders == {} and notes.tree().notes == {}


def test_a_root_that_is_not_there_reads_as_an_empty_tree(tmp_path: Path) -> None:
    """It is made when something is first written, so until then there is nothing."""
    scanner = NotesScanner(tmp_path / "absent")
    assert scanner.tree().notes == {}


def test_a_file_at_the_root_is_listed(notes: NotesScanner, root: Path) -> None:
    make_note(root, "scratch.ink", "Once.\n")
    note = next(iter(notes.tree().notes.values()))
    assert (note.name, note.folder_id) == ("scratch", None)


def test_a_directory_needs_no_manifest_to_be_a_folder(
    notes: NotesScanner, root: Path
) -> None:
    """Unlike a story, a folder here is a folder because it is a directory."""
    make_folder(root, "research")
    assert only_folder(notes).name == "research"


def test_a_folder_is_named_by_its_manifest_where_it_has_one(
    notes: NotesScanner, root: Path
) -> None:
    make_folder(root, "research", title="Research Notes")
    assert only_folder(notes).name == "Research Notes"


def test_a_folder_carries_the_context_its_manifest_gives_it(
    notes: NotesScanner, root: Path
) -> None:
    make_folder(root, "research", context="Background reading.")
    assert only_folder(notes).context == "Background reading."


def test_a_folder_with_no_manifest_has_no_context(
    notes: NotesScanner, root: Path
) -> None:
    make_folder(root, "research")
    assert only_folder(notes).context == ""


def test_the_files_in_a_folder_belong_to_it(notes: NotesScanner, root: Path) -> None:
    folder = make_folder(root, "research")
    make_note(folder, "worldbuilding.ink")

    listed = only_folder(notes)
    assert [note.name for note in listed.notes] == ["worldbuilding"]
    assert listed.notes[0].folder_id == listed.id


def test_only_ink_files_are_files(notes: NotesScanner, root: Path) -> None:
    make_note(root, "scratch.ink")
    make_note(root, "notes.md")
    make_note(root, "todo.txt")
    assert [note.filename for note in notes.tree().notes.values()] == ["scratch.ink"]


def test_a_manifest_is_not_a_file(notes: NotesScanner, root: Path) -> None:
    make_folder(root, "research", context="Background.")
    assert only_folder(notes).notes == ()


def test_a_folder_inside_a_folder_is_not_listed(notes: NotesScanner, root: Path) -> None:
    """One level, as the flat tree the API answers can express."""
    folder = make_folder(root, "research")
    make_folder(folder, "japan")
    make_note(folder / "japan", "edo.ink")

    assert [f.slug for f in notes.tree().folders.values()] == ["research"]
    assert notes.tree().notes == {}


def test_a_symlinked_folder_is_not_listed(notes: NotesScanner, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "research").symlink_to(outside, target_is_directory=True)
    assert notes.tree().folders == {}


def test_a_symlinked_file_is_not_listed(notes: NotesScanner, root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.ink"
    outside.write_text("Once.\n", encoding="utf-8")
    (root / "scratch.ink").symlink_to(outside)
    assert notes.tree().notes == {}


def test_a_file_is_named_by_its_header(notes: NotesScanner, root: Path) -> None:
    """A note keeps the name that was typed wherever it sits, root included."""
    make_note(root, "scratch.ink", "---\ntitle: Scratch Pad\n---\nOnce.\n")
    assert [n.name for n in notes.tree().notes.values()] == ["Scratch Pad"]


def test_a_file_carries_the_status_and_summary_its_header_gives_it(
    notes: NotesScanner, root: Path
) -> None:
    make_note(root, "scratch.ink", "---\nstatus: draft\nsummary: A list.\n---\nOnce.\n")
    note = next(iter(notes.tree().notes.values()))
    assert (note.status, note.summary) == ("draft", "A list.")


def test_an_unparseable_manifest_does_not_hide_the_files(
    notes: NotesScanner, root: Path
) -> None:
    folder = make_folder(root, "research")
    (folder / "manifest.yaml").write_text("title: [oops\n", encoding="utf-8")
    make_note(folder, "worldbuilding.ink")

    listed = only_folder(notes)
    assert listed.name == "research"
    assert [note.name for note in listed.notes] == ["worldbuilding"]


# --- ids --------------------------------------------------------------------


def test_a_file_and_a_folder_of_the_same_name_have_different_ids(
    notes: NotesScanner, root: Path
) -> None:
    make_folder(root, "research")
    make_note(root, "research.ink")

    scan = notes.tree()
    assert set(scan.folders) & set(scan.notes) == set()


def test_an_id_is_derived_from_the_path_inside_this_root(
    notes: NotesScanner, root: Path
) -> None:
    folder = make_folder(root, "research")
    make_note(folder, "worldbuilding.ink")

    note = next(iter(notes.tree().notes.values()))
    assert note.id == derive_id(SPACE, "research/worldbuilding.ink")


def test_an_id_survives_a_new_scanner(notes: NotesScanner, root: Path) -> None:
    make_note(root, "scratch.ink")
    first = set(notes.tree().notes)
    assert set(NotesScanner(root).tree().notes) == first


# --- containment ------------------------------------------------------------


def test_a_path_out_of_the_root_is_refused(notes: NotesScanner) -> None:
    with pytest.raises(StorageError, match="outside"):
        notes.path(PurePosixPath("../elsewhere.ink"))


def test_a_symlink_leading_out_of_the_root_is_refused(
    notes: NotesScanner, root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "elsewhere.ink"
    outside.write_text("Once.\n", encoding="utf-8")
    (root / "link.ink").symlink_to(outside)

    with pytest.raises(StorageError, match="outside"):
        notes.path(PurePosixPath("link.ink"))


# --- the held scan ----------------------------------------------------------


def test_a_second_scan_is_the_held_one(notes: NotesScanner, root: Path) -> None:
    make_note(root, "scratch.ink")
    assert notes.tree() is notes.tree()


def test_a_new_file_on_disk_is_picked_up(notes: NotesScanner, root: Path) -> None:
    make_note(root, "scratch.ink")
    assert len(notes.tree().notes) == 1

    make_note(root, "other.ink")
    assert len(notes.tree().notes) == 2


def test_a_retitled_file_is_picked_up(notes: NotesScanner, root: Path) -> None:
    path = make_note(root, "scratch.ink", "Once.\n")
    assert [n.name for n in notes.tree().notes.values()] == ["scratch"]

    path.write_text("---\ntitle: Scratch Pad\n---\nOnce.\n", encoding="utf-8")
    os.utime(path, ns=(0, 1))
    assert [n.name for n in notes.tree().notes.values()] == ["Scratch Pad"]


def test_a_rewritten_manifest_is_picked_up(notes: NotesScanner, root: Path) -> None:
    folder = make_folder(root, "research", context="Before.")
    assert only_folder(notes).context == "Before."

    manifest = folder / "manifest.yaml"
    manifest.write_text("context: After.\n", encoding="utf-8")
    os.utime(manifest, ns=(0, 1))
    assert only_folder(notes).context == "After."


def test_writing_a_file_leaves_the_held_scan_alone(notes: NotesScanner, root: Path) -> None:
    make_note(root, "scratch.ink")
    before = notes.tree()

    notes.write_note(next(iter(before.notes)), "Once.\n")
    assert notes.tree() is before


# --- changes ----------------------------------------------------------------


def test_a_created_folder_is_a_directory_and_nothing_else(
    notes: NotesScanner, root: Path
) -> None:
    """Nothing to record, so no manifest: the directory says all there is to say."""
    folder = notes.create_folder("research")

    assert (root / "research").is_dir()
    assert list((root / "research").iterdir()) == []
    assert (folder.name, folder.context) == ("research", "")


def test_a_created_folder_records_a_name_its_slug_cannot_say(
    notes: NotesScanner, root: Path
) -> None:
    notes.create_folder("Research Notes")

    document = yaml.safe_load(
        (root / "research-notes" / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert document == {"title": "Research Notes"}


def test_a_created_folder_records_its_context(notes: NotesScanner, root: Path) -> None:
    notes.create_folder("research", "Background reading.")

    document = yaml.safe_load(
        (root / "research" / "manifest.yaml").read_text(encoding="utf-8")
    )
    assert document == {"context": "Background reading."}


def test_a_root_that_is_not_there_is_made_on_the_first_write(tmp_path: Path) -> None:
    scanner = NotesScanner(tmp_path / "files")
    scanner.create_folder("research")
    assert (tmp_path / "files" / "research").is_dir()


def test_two_folders_may_share_a_name(notes: NotesScanner) -> None:
    first = notes.create_folder("Research")
    second = notes.create_folder("Research")
    assert first.id != second.id
    assert {first.slug, second.slug} == {"research", "research-2"}


def test_giving_a_folder_a_context_writes_the_manifest_it_had_none_of(
    notes: NotesScanner, root: Path
) -> None:
    folder = notes.create_folder("research")
    assert not (root / "research" / "manifest.yaml").exists()

    notes.update_folder(folder.id, context="Background reading.")

    assert yaml.safe_load(
        (root / "research" / "manifest.yaml").read_text(encoding="utf-8")
    ) == {"context": "Background reading."}


def test_emptying_a_context_takes_the_manifest_away_again(
    notes: NotesScanner, root: Path
) -> None:
    """Nothing left to keep, so nothing is kept beside the folder."""
    folder = notes.create_folder("research", "Background reading.")
    notes.update_folder(folder.id, context="")
    assert not (root / "research" / "manifest.yaml").exists()


def test_renaming_a_folder_keeps_its_id_and_its_slug(
    notes: NotesScanner, root: Path
) -> None:
    """The files in it keep their ids, as the chapters of a retitled story do."""
    folder = notes.create_folder("research")
    note = notes.create_note(folder.id, "worldbuilding")

    renamed = notes.update_folder(folder.id, name="Research Notes")

    assert renamed.id == folder.id
    assert renamed.slug == "research"
    assert renamed.name == "Research Notes"
    assert notes.note(note.id).id == note.id


def test_renaming_a_folder_to_its_slug_takes_the_manifest_away(
    notes: NotesScanner, root: Path
) -> None:
    folder = notes.create_folder("Research Notes")
    notes.update_folder(folder.id, name="research-notes")
    assert not (root / "research-notes" / "manifest.yaml").exists()


def test_renaming_an_unknown_folder_is_not_found(notes: NotesScanner) -> None:
    with pytest.raises(NotFound):
        notes.update_folder("0" * 16, name="Research")


def test_deleting_a_folder_takes_its_files_and_its_manifest(
    notes: NotesScanner, root: Path
) -> None:
    folder = notes.create_folder("research", "Background.")
    notes.create_note(folder.id, "worldbuilding")

    notes.delete_folder(folder.id)

    assert not (root / "research").exists()
    assert notes.tree().notes == {}


def test_a_folder_holding_anything_else_is_not_deleted(
    notes: NotesScanner, root: Path
) -> None:
    """Whatever it is was put there by hand, and is not a button's to remove."""
    folder = notes.create_folder("research")
    (root / "research" / "sources.md").write_text("A list.\n", encoding="utf-8")

    with pytest.raises(Conflict, match="sources.md"):
        notes.delete_folder(folder.id)
    assert (root / "research").is_dir()


def test_a_created_file_at_the_root_is_a_header_and_no_prose(
    notes: NotesScanner, root: Path
) -> None:
    note = notes.create_note(None, "Scratch Pad")

    assert (root / "scratch-pad.ink").read_text(encoding="utf-8") == (
        "---\ntitle: Scratch Pad\n---\n"
    )
    assert (note.name, note.folder_id) == ("Scratch Pad", None)


def test_a_created_file_in_a_folder_belongs_to_it(notes: NotesScanner, root: Path) -> None:
    folder = notes.create_folder("research")
    note = notes.create_note(folder.id, "Worldbuilding")

    assert (root / "research" / "worldbuilding.ink").is_file()
    assert note.folder_id == folder.id


def test_a_file_named_like_its_filename_gets_no_header(
    notes: NotesScanner, root: Path
) -> None:
    notes.create_note(None, "scratch")
    assert (root / "scratch.ink").read_text(encoding="utf-8") == ""


def test_a_file_in_an_unknown_folder_is_not_found(notes: NotesScanner) -> None:
    with pytest.raises(NotFound):
        notes.create_note("0" * 16, "Scratch")


def test_renaming_a_file_renames_it_and_changes_its_id(
    notes: NotesScanner, root: Path
) -> None:
    note = notes.create_note(None, "Scratch")
    renamed = notes.update_note(note.id, name="Scratch Pad", folder_id=None)

    assert renamed.id != note.id
    assert renamed.filename == "scratch-pad.ink"
    assert not (root / "scratch.ink").exists()


def test_renaming_a_file_writes_the_name_into_its_header(
    notes: NotesScanner, root: Path
) -> None:
    note = notes.create_note(None, "Scratch")
    notes.write_note(note.id, "Once.\n")

    renamed = notes.update_note(note.id, name="Scratch Pad", folder_id=None)

    assert (root / renamed.filename).read_text(encoding="utf-8") == (
        "---\ntitle: Scratch Pad\n---\nOnce.\n"
    )


def test_moving_a_file_into_a_folder(notes: NotesScanner, root: Path) -> None:
    folder = notes.create_folder("research")
    note = notes.create_note(None, "Worldbuilding")

    moved = notes.update_note(note.id, folder_id=folder.id)

    assert moved.folder_id == folder.id
    assert (root / "research" / "worldbuilding.ink").is_file()
    assert not (root / "worldbuilding.ink").exists()


def test_moving_a_file_back_out_to_the_root(notes: NotesScanner, root: Path) -> None:
    """A file here has somewhere to go that a chapter does not."""
    folder = notes.create_folder("research")
    note = notes.create_note(folder.id, "Worldbuilding")

    moved = notes.update_note(note.id, folder_id=None)

    assert moved.folder_id is None
    assert (root / "worldbuilding.ink").is_file()
    assert not (root / "research" / "worldbuilding.ink").exists()


def test_a_file_moved_onto_a_taken_filename_is_numbered(notes: NotesScanner) -> None:
    folder = notes.create_folder("research")
    notes.create_note(folder.id, "Worldbuilding")
    other = notes.create_note(None, "Worldbuilding")

    moved = notes.update_note(other.id, folder_id=folder.id)

    assert moved.filename == "worldbuilding-2.ink"
    assert moved.name == "Worldbuilding"


def test_a_rename_that_leaves_the_filename_alone_still_renames(
    notes: NotesScanner,
) -> None:
    note = notes.create_note(None, "scratch")
    renamed = notes.update_note(note.id, name="Scratch", folder_id=None)

    assert renamed.id == note.id
    assert renamed.filename == "scratch.ink"
    assert renamed.name == "Scratch"


def test_deleting_a_file_removes_it(notes: NotesScanner, root: Path) -> None:
    note = notes.create_note(None, "Scratch")
    notes.delete_note(note.id)

    assert not (root / "scratch.ink").exists()
    assert notes.tree().notes == {}


def test_deleting_an_unknown_file_is_not_found(notes: NotesScanner) -> None:
    with pytest.raises(NotFound):
        notes.delete_note("0" * 16)


# --- content ----------------------------------------------------------------


def test_a_file_reads_back_what_was_written(notes: NotesScanner) -> None:
    note = notes.create_note(None, "Scratch")
    notes.write_note(note.id, "A list.\n")
    assert notes.read_note(note.id) == "A list.\n"


def test_reading_a_file_leaves_out_its_header(notes: NotesScanner, root: Path) -> None:
    make_note(root, "scratch.ink", "---\ntitle: Scratch Pad\n---\nA list.\n")
    note = next(iter(notes.tree().notes.values()))
    assert notes.read_note(note.id) == "A list.\n"


def test_writing_a_file_keeps_its_header(notes: NotesScanner, root: Path) -> None:
    make_note(root, "scratch.ink", "---\ntitle: Scratch Pad\nstatus: draft\n---\nOne.\n")
    note = next(iter(notes.tree().notes.values()))

    notes.write_note(note.id, "Two.\n")

    assert (root / "scratch.ink").read_text(encoding="utf-8") == (
        "---\ntitle: Scratch Pad\nstatus: draft\n---\nTwo.\n"
    )


def test_a_file_deleted_under_the_client_is_not_found(
    notes: NotesScanner, root: Path
) -> None:
    note = notes.create_note(None, "Scratch")
    (root / "scratch.ink").unlink()

    with pytest.raises(NotFound):
        notes.read_note(note.id)


def test_a_file_that_is_not_utf8_says_so(notes: NotesScanner, root: Path) -> None:
    make_note(root, "scratch.ink")
    (root / "scratch.ink").write_bytes(b"\xff\xfe")
    note = next(iter(notes.tree().notes.values()))

    with pytest.raises(StorageError, match="UTF-8"):
        notes.read_note(note.id)
