# -*- coding: utf-8 -*-
"""The files that are not a novel: notes, and the folders they sit in.

A second root, outside the story repository and never committed, holding `.ink` files
one level deep:

    scratch.ink
    research/manifest.yaml          this folder's name and context
    research/worldbuilding.ink

A directory here is a directory and nothing more. Unlike a story, it needs no manifest
to exist, and `manifest.yaml` is written only when there is something to keep in it —
a name a slug cannot spell, or a context. A folder inside a folder is not listed.

A file is a file if it ends in `.ink`, so the format is the one `ink.py` describes and
the name a note is shown under comes from its own header, wherever it sits.

Ids are derived from the path relative to this root, in this space, so nothing here can
collide with a story or a chapter.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import ink
from .fs import (
    Conflict,
    HeldScan,
    NotFound,
    derive_id,
    directories,
    dump_yaml,
    files_with_suffix,
    free_path,
    mtime,
    read_text,
    read_yaml_file,
    resolve_within,
    slugify,
    write_atomically,
)

#: Which root an id belongs to. The same relative path under the stories is a
#: different file and derives a different id.
SPACE = "notes"

MANIFEST = "manifest.yaml"


@dataclass(frozen=True)
class Note:
    """One `.ink` file, at the root or in a folder."""

    id: str
    #: Relative to the root of this space.
    relpath: PurePosixPath
    #: The header's title, or the filename without its suffix.
    name: str
    status: str
    summary: str
    #: The folder holding it, or `None` for one sitting at the root.
    folder_id: str | None

    @property
    def filename(self) -> str:
        """The file's own name."""
        return self.relpath.name


@dataclass(frozen=True)
class Folder:
    """One directory in this space, with the notes found in it."""

    id: str
    slug: str
    relpath: PurePosixPath
    name: str
    context: str
    notes: tuple[Note, ...]


@dataclass(frozen=True)
class NoteTree:
    """One scan of this space, indexed by id."""

    folders: dict[str, Folder]
    #: Every note, at the root or in a folder.
    notes: dict[str, Note]


def read_manifest(path: Path) -> dict:
    """The parsed `manifest.yaml`, or an empty mapping if it cannot be read.

    A folder whose manifest has been hand-edited into something unparseable keeps its
    slug as a name and stays reachable, with the files in it listed as ever.
    """
    return read_yaml_file(path)


def write_manifest(path: Path, document: dict) -> None:
    """Replaces `manifest.yaml` with `document`, or removes it if there is nothing to say."""
    if document:
        write_atomically(path, dump_yaml(document))
    else:
        path.unlink(missing_ok=True)


def folder_document(slug: str, name: str, context: str) -> dict:
    """What a folder's manifest should hold, which is nothing at all where it can be.

    A folder named after its own slug, with no context, needs no file: the directory
    already says everything there is to know about it.
    """
    document: dict = {}
    if name != slug:
        document["title"] = name
    if context:
        document["context"] = context
    return document


class NotesScanner:
    """Reads this space, and makes the changes the API is asked for.

    A scan lists the root and each folder in it. The result is held until the files, a
    manifest or a header change. Writing a note's prose leaves the held scan valid: it
    changes no name and no path.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._held = HeldScan(self._scan, self._current_stamp)

    # --- reading -----------------------------------------------------------

    def tree(self) -> NoteTree:
        """The current scan, rebuilt first if anything it reads has changed."""
        return self._held.get()

    def invalidate(self) -> None:
        """Drops the held scan, after a change this process made."""
        self._held.drop()

    def folder(self, folder_id: str) -> Folder:
        """The folder with that id, or `NotFound`."""
        folder = self.tree().folders.get(folder_id)
        if folder is None:
            raise NotFound(f'No folder with id "{folder_id}".')
        return folder

    def note(self, note_id: str) -> Note:
        """The note with that id, or `NotFound`."""
        note = self.tree().notes.get(note_id)
        if note is None:
            raise NotFound(f'No file with id "{note_id}".')
        return note

    def path(self, relpath: PurePosixPath) -> Path:
        """The absolute path of something in this space."""
        return resolve_within(self.root, relpath, "files root")

    def _folders(self) -> list[Path]:
        """The directories at the root, in name order. A symlinked one is not listed."""
        return directories(self.root)

    @staticmethod
    def _note_files(directory: Path) -> list[Path]:
        """The files in one directory, in filename order."""
        return files_with_suffix(directory, ink.SUFFIX)

    def _current_stamp(self) -> tuple:
        """What the held scan is checked against: which files exist, and when each
        file the scan reads last changed.

        Listing rather than trusting a directory's mtime is deliberate, for the reason
        `storage.Scanner` gives: a file written within one filesystem tick of the last
        scan would otherwise never be noticed.
        """
        return (
            tuple((file.name, mtime(file)) for file in self._note_files(self.root)),
            tuple(
                (
                    folder.name,
                    mtime(folder / MANIFEST),
                    tuple(
                        (file.name, mtime(file))
                        for file in self._note_files(folder)
                    ),
                )
                for folder in self._folders()
            ),
        )

    def _note(self, relpath: PurePosixPath, path: Path, folder_id: str | None) -> Note:
        header = ink.read_header(path)
        return Note(
            id=derive_id(SPACE, str(relpath)),
            relpath=relpath,
            name=ink.display_name(header, path.stem),
            status=str(header.get("status") or ""),
            summary=str(header.get("summary") or ""),
            folder_id=folder_id,
        )

    def _scan(self) -> NoteTree:
        folders: dict[str, Folder] = {}
        notes: dict[str, Note] = {}

        for file in self._note_files(self.root):
            note = self._note(PurePosixPath(file.name), file, None)
            notes[note.id] = note

        for directory in self._folders():
            slug = directory.name
            relpath = PurePosixPath(slug)
            folder_id = derive_id(SPACE, slug)
            document = read_manifest(directory / MANIFEST)

            own: list[Note] = []
            for file in self._note_files(directory):
                note = self._note(relpath / file.name, file, folder_id)
                own.append(note)
                notes[note.id] = note

            own.sort(key=lambda note: note.name)
            folders[folder_id] = Folder(
                id=folder_id,
                slug=slug,
                relpath=relpath,
                name=str(document.get("title") or slug),
                context=str(document.get("context") or ""),
                notes=tuple(own),
            )

        return NoteTree(folders=folders, notes=notes)

    # --- writing -----------------------------------------------------------

    def create_folder(self, name: str, context: str = "") -> Folder:
        """Creates a directory, with a manifest only if there is something to put in it."""
        self.root.mkdir(parents=True, exist_ok=True)
        directory = free_path(self.root, slugify(name, "folder"))
        directory.mkdir()
        write_manifest(
            directory / MANIFEST, folder_document(directory.name, name, context)
        )

        self.invalidate()
        return self.folder(derive_id(SPACE, directory.name))

    def update_folder(
        self, folder_id: str, *, name: str | None = None, context: str | None = None
    ) -> Folder:
        """Renames a folder, or rewrites its context. The directory keeps its slug.

        The id is derived from the path, so it does not change here and the notes in
        the folder keep theirs.
        """
        folder = self.folder(folder_id)
        document = folder_document(
            folder.slug,
            name if name is not None else folder.name,
            context if context is not None else folder.context,
        )
        write_manifest(self.path(folder.relpath / MANIFEST), document)

        self.invalidate()
        return self.folder(folder_id)

    def delete_folder(self, folder_id: str) -> None:
        """Deletes a folder with its notes and its manifest, and nothing else.

        Refused while the directory holds anything more, since whatever that is was put
        there by hand and is not this endpoint's to remove.
        """
        folder = self.folder(folder_id)
        directory = self.path(folder.relpath)

        extra = sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.name != MANIFEST and entry.suffix != ink.SUFFIX
        )
        if extra:
            raise Conflict(
                f'"{folder.name}" also holds {", ".join(extra)}, which deleting it '
                "would remove. Delete those first."
            )

        shutil.rmtree(directory)
        self.invalidate()

    def _directory_of(self, folder_id: str | None) -> tuple[Path, PurePosixPath]:
        """Where a note with that folder belongs, absolute and relative."""
        if folder_id is None:
            return self.root, PurePosixPath()
        folder = self.folder(folder_id)
        return self.path(folder.relpath), folder.relpath

    def create_note(self, folder_id: str | None, name: str) -> Note:
        """Creates an `.ink` file with no prose, in a folder or at the root.

        As with a chapter, the file is named after a slug of `name` and its header
        records the name where the slug cannot spell it back.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        directory, relative = self._directory_of(folder_id)
        directory.mkdir(parents=True, exist_ok=True)

        file = free_path(directory, slugify(name, "file"), ink.SUFFIX)
        header = {"title": name} if name != file.stem else {}
        write_atomically(file, ink.render(header, ""))

        self.invalidate()
        return self.note(derive_id(SPACE, str(relative / file.name)))

    def update_note(
        self, note_id: str, *, name: str | None = None, folder_id: str | None
    ) -> Note:
        """Renames a note, moves it to a folder or to the root, or both.

        `folder_id` says where the note should end up, `None` meaning the root, so a
        caller that means to leave it where it is passes the folder it is already in.

        The note's id changes whenever its path does, which a rename or a move both do.
        """
        note = self.note(note_id)
        display = name if name is not None else note.name

        source = self.path(note.relpath)
        target_dir, target_relative = self._directory_of(folder_id)
        target_dir.mkdir(parents=True, exist_ok=True)

        stem = slugify(name, "file") if name is not None else source.stem
        if target_dir == source.parent and stem == source.stem:
            # The new name slugs to the file's own name, so only the header can
            # change. Renaming here would take the file to <stem>-2.ink.
            target = source
        else:
            target = free_path(target_dir, stem, ink.SUFFIX)
            source.rename(target)

        target_relpath = target_relative / target.name
        if name is not None:
            self._retitle(target_relpath, display)

        self.invalidate()
        return self.note(derive_id(SPACE, str(target_relpath)))

    def _retitle(self, relpath: PurePosixPath, name: str) -> None:
        """Writes `name` into a file's header as its title."""
        text = ink.with_title(ink.parse(self._text(relpath)), name, relpath.stem)
        if text is not None:
            write_atomically(self.path(relpath), text)

    def delete_note(self, note_id: str) -> None:
        """Deletes a note's file."""
        note = self.note(note_id)
        self.path(note.relpath).unlink(missing_ok=True)
        self.invalidate()

    # --- content -----------------------------------------------------------

    def _text(self, relpath: PurePosixPath) -> str:
        """A file's text as it is on disk, header and all."""
        return read_text(self.path(relpath), relpath)

    def read_note(self, note_id: str) -> str:
        """A note's prose, without the header above it."""
        note = self.note(note_id)
        return ink.parse(self._text(note.relpath)).body

    def write_note(self, note_id: str, body: str) -> None:
        """Replaces a note's prose, keeping the header the file has."""
        note = self.note(note_id)
        path = self.path(note.relpath)
        if not path.is_file():
            raise NotFound(f'"{note.relpath}" is no longer on disk.')

        document = ink.parse(self._text(note.relpath))
        write_atomically(path, ink.render(document.metadata, body))
        self._held.restamp()
