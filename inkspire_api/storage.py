# -*- coding: utf-8 -*-
"""The stories on disk, and the ids the API addresses them by.

Most stories are one directory each, holding a `story.yaml` and a `chapters/` directory
of `.ink` files:

    stories/<story-slug>/story.yaml
    stories/<story-slug>/chapters/<chapter-slug>.ink

A directory is a story if and only if it holds a `story.yaml`. That file gives the
story its title and synopsis, and the order its chapters are read in. The filesystem
determines which chapters exist, and each chapter's own header gives it its title,
status and summary. Anything else in a story directory — a `lorebook/`, a
`timeline.yaml` — is not a chapter and is not listed.

A loose `.ink` file sitting directly at the stories root, with no directory around it,
is a one-shot: a story with exactly one chapter and no manifest.

    stories/<one-shot-slug>.ink

Its own header carries the same title, status and summary a chapter's header does —
there is nothing for a `story.yaml` to add when there is only one chapter and no order
to record.

A client names a story, a chapter or a one-shot by an id derived from its path, never
by the path, so no request can point at a location on disk. Ids need no table and
survive a restart. Renaming a chapter or a one-shot changes its id, because it is then
a different path.
"""

from __future__ import annotations

import copy
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import ink
from .entries import File, Folder
from .fs import (
    Conflict,
    HeldScan,
    Malformed,
    NotFound,
    StorageError,
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

#: Which root an id belongs to. The same relative path in the other root is a
#: different file and derives a different id.
SPACE = "stories"

STORIES = "stories"
CHAPTERS = "chapters"
MANIFEST = "story.yaml"
CHAPTER_SUFFIX = ink.SUFFIX

#: Authored by hand, read by the `timeline` and `lorebook` commands and by the routes
#: in `timelines.py` and `lore.py`, and never written here. Their presence is what says
#: which of the two views a story can offer.
TIMELINE = "timeline.yaml"
LOREBOOK = "lorebook"
LOREBOOK_MANIFEST = "lorebook.yaml"

#: Placed in a chapters directory that has none yet, because git stores no empty
#: directory and the story would arrive at a clone without one.
KEEP = ".gitkeep"


class DataRootMissing(StorageError):
    """The story repository is not present where it is configured to be."""


@dataclass(frozen=True)
class Chapter(File):
    """One `.ink` file in a story's chapters directory."""

    story_id: str


#: A loose `.ink` file at the stories root -- a story with exactly one chapter and no
#: manifest. It is nothing more than a `File`: everything a chapter's own header
#: already carries -- title, status, summary -- is what a one-shot has too, and there
#: is nothing for a `story.yaml` to add. Distinguished from a chapter only by which
#: scan found it, the same way a note at a folder's root and one inside it are both
#: just `Note`, distinguished only by `folder_id`.
OneShot = File


@dataclass(frozen=True)
class Story(Folder):
    """One story directory, with the chapters found in it."""

    #: Whether `timeline.yaml` and `lorebook/lorebook.yaml` are here. Neither is read
    #: by the scan; a client asks for them on their own routes.
    has_timeline: bool = False
    has_lorebook: bool = False


@dataclass(frozen=True)
class Tree:
    """One scan of the repository, indexed by id."""

    stories: dict[str, Story]
    chapters: dict[str, Chapter]
    one_shots: dict[str, OneShot]


def read_manifest(path: Path) -> dict:
    """The parsed `story.yaml`, or an empty mapping if it cannot be read.

    A manifest hand-edited into something unparseable must not hide the chapters beside
    it, so the story keeps its slug as a name and stays reachable.
    """
    return read_yaml_file(path)


def write_manifest(path: Path, document: dict) -> None:
    """Replaces `story.yaml` with `document`, keys in the order they were inserted."""
    write_atomically(path, dump_yaml(document))


def chapter_list(document: dict) -> list:
    """The manifest's chapter entries. An absent or malformed list reads as empty."""
    listed = document.get(CHAPTERS)
    return listed if isinstance(listed, list) else []


def entry_for(listed: list, filename: str) -> dict | None:
    """The manifest entry naming `filename`, if one does."""
    for entry in listed:
        if isinstance(entry, dict) and str(entry.get("file", "")) == filename:
            return entry
    return None


def _remove_entry(listed: list, filename: str) -> None:
    entry = entry_for(listed, filename)
    if entry is not None:
        listed.remove(entry)


def _position_in(document: dict) -> Callable[[Chapter], int]:
    """Sorts chapters into the order `document` lists them in, unlisted ones last.

    The manifest decides the order and the filesystem decides what exists, so a chapter
    the manifest does not name still has a place: after every chapter it does. Ties are
    left to the caller's sort being stable, which keeps unlisted chapters in the filename
    order they were found in.
    """
    order = {
        str(entry.get("file", "")): position
        for position, entry in enumerate(chapter_list(document))
        if isinstance(entry, dict)
    }
    return lambda chapter: order.get(chapter.filename, len(order))


def ensure_entry(listed: list, filename: str) -> None:
    """Gives `filename` a place in the order, last, if the manifest does not list it.

    A chapter this API creates or moves takes a place as it arrives. One that arrives
    another way — a `git pull`, an editor — is listed by nothing, and is shown after
    the chapters that are listed.
    """
    if entry_for(listed, filename) is None:
        listed.append({"file": filename})


class Scanner:
    """Reads the repository, and makes the changes the API is asked for.

    A scan walks `stories/`, parses each `story.yaml` and lists each `chapters/`, and
    also lists the one-shots sitting loose at the root. The result is held until the
    files or a manifest change, so repeated requests do not reparse unchanged YAML.
    Writing a chapter's or a one-shot's content leaves the held scan valid: it changes
    no name and no path.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self._held = HeldScan(self._scan, self._current_stamp)

    # --- reading -----------------------------------------------------------

    @property
    def stories_dir(self) -> Path:
        """The directory the stories sit in."""
        return self.root / STORIES

    def tree(self) -> Tree:
        """The current scan, rebuilt first if the repository has changed."""
        return self._held.get()

    def invalidate(self) -> None:
        """Drops the held scan, after a change this process made."""
        self._held.drop()

    def story(self, story_id: str) -> Story:
        """The story with that id, or `NotFound`."""
        story = self.tree().stories.get(story_id)
        if story is None:
            raise NotFound(f'No story with id "{story_id}".')
        return story

    def chapter(self, chapter_id: str) -> Chapter:
        """The chapter with that id, or `NotFound`."""
        chapter = self.tree().chapters.get(chapter_id)
        if chapter is None:
            raise NotFound(f'No chapter with id "{chapter_id}".')
        return chapter

    def one_shot(self, one_shot_id: str) -> OneShot:
        """The one-shot with that id, or `NotFound`."""
        one_shot = self.tree().one_shots.get(one_shot_id)
        if one_shot is None:
            raise NotFound(f'No one-shot with id "{one_shot_id}".')
        return one_shot

    def file(self, file_id: str) -> File:
        """Whichever kind of file this id names -- a chapter or a one-shot -- or
        `NotFound`. A route that only cares about a file's content, its header fields
        or its path resolves through here rather than picking a lookup itself. A
        `Chapter` is a `File` too, so the caller narrows with `isinstance` only if it
        needs the chapter-specific `story_id`."""
        tree = self.tree()
        found = tree.chapters.get(file_id) or tree.one_shots.get(file_id)
        if found is None:
            raise NotFound(f'No file with id "{file_id}".')
        return found

    def path(self, relpath: PurePosixPath) -> Path:
        """The absolute path of something in the repository."""
        return resolve_within(self.root, relpath, "story repository")

    def _story_dirs(self) -> list[Path]:
        """The story directories, in name order. A symlinked directory is not one."""
        return directories(self.stories_dir)

    @staticmethod
    def _one_shot_files(stories_dir: Path) -> list[Path]:
        """The one-shots sitting loose at the stories root, in filename order. The
        direct counterpart to `_chapter_files`, applied to the root itself rather than
        a story's own `chapters/` directory."""
        return files_with_suffix(stories_dir, CHAPTER_SUFFIX)

    def _current_stamp(self) -> tuple:
        """What the held scan is checked against: which files exist, and when each
        file that the scan reads last changed.

        Listing the directories rather than trusting their mtimes is deliberate. A
        filesystem timestamp advances in ticks of about a millisecond, so a chapter
        written within one tick of the previous scan would leave the mtime it was
        scanned at and never be noticed.

        The manifests and the chapters are compared by mtime, since reading them is
        the work the held scan exists to avoid, and a chapter retitled in its own
        header changes no name and no path. Rewriting one within a tick of a scan is
        therefore missed; a change made through this class invalidates the scan
        outright. A one-shot is compared the same way, having no manifest of its own.

        The timeline and the lorebook are here by presence rather than by mtime: the
        scan records only whether each exists, and editing one does not change that.
        Writing a story its first `timeline.yaml` has to show up, though, so a scan
        taken before it cannot be kept.
        """
        return (
            tuple(
                (
                    story_dir.name,
                    mtime(story_dir / MANIFEST),
                    (story_dir / TIMELINE).is_file(),
                    (story_dir / LOREBOOK / LOREBOOK_MANIFEST).is_file(),
                    tuple(
                        (file.name, mtime(file))
                        for file in self._chapter_files(story_dir / CHAPTERS)
                    ),
                )
                for story_dir in self._story_dirs()
            ),
            tuple(
                (file.name, mtime(file))
                for file in self._one_shot_files(self.stories_dir)
            ),
        )

    def _scan(self) -> Tree:
        stories: dict[str, Story] = {}
        chapters: dict[str, Chapter] = {}

        for story_dir in self._story_dirs():
            manifest = story_dir / MANIFEST
            if not manifest.is_file():
                continue

            slug = story_dir.name
            relpath = PurePosixPath(STORIES) / slug
            story_id = derive_id(SPACE, str(relpath))
            document = read_manifest(manifest)

            own: list[Chapter] = []
            for file in self._chapter_files(story_dir / CHAPTERS):
                chapter_relpath = relpath / CHAPTERS / file.name
                header = ink.read_header(file)
                chapter = Chapter(
                    id=derive_id(SPACE, str(chapter_relpath)),
                    relpath=chapter_relpath,
                    name=ink.display_name(header, file.stem),
                    status=str(header.get("status") or ""),
                    summary=str(header.get("summary") or ""),
                    story_id=story_id,
                )
                own.append(chapter)
                chapters[chapter.id] = chapter

            own.sort(key=_position_in(document))
            stories[story_id] = Story(
                id=story_id,
                slug=slug,
                relpath=relpath,
                name=str(document.get("title") or slug),
                summary=str(document.get("synopsis") or ""),
                files=tuple(own),
                has_timeline=(story_dir / TIMELINE).is_file(),
                has_lorebook=(story_dir / LOREBOOK / LOREBOOK_MANIFEST).is_file(),
            )

        return Tree(stories=stories, chapters=chapters, one_shots=self._one_shots())

    def _one_shots(self) -> dict[str, OneShot]:
        """The one-shots at the stories root, indexed by id."""
        one_shots: dict[str, OneShot] = {}
        for file in self._one_shot_files(self.stories_dir):
            relpath = PurePosixPath(STORIES) / file.name
            header = ink.read_header(file)
            one_shot = OneShot(
                id=derive_id(SPACE, str(relpath)),
                relpath=relpath,
                name=ink.display_name(header, file.stem),
                status=str(header.get("status") or ""),
                summary=str(header.get("summary") or ""),
            )
            one_shots[one_shot.id] = one_shot
        return one_shots

    @staticmethod
    def _chapter_files(chapters_dir: Path) -> list[Path]:
        """The chapters in a story, in filename order."""
        return files_with_suffix(chapters_dir, CHAPTER_SUFFIX)

    # --- writing -----------------------------------------------------------

    def _require_root(self) -> Path:
        if not self.root.is_dir():
            raise DataRootMissing(
                f"The story repository is not at {self.root}. Clone it there, or set "
                "INKSPIRE_DATA_ROOT to where it is."
            )
        return self.root

    def create_story(self, name: str, summary: str = "") -> Story:
        """Creates `stories/<slug>/` with a manifest and an empty chapters directory."""
        self._require_root()
        self.stories_dir.mkdir(parents=True, exist_ok=True)

        story_dir = free_path(self.stories_dir, slugify(name, "story"))
        (story_dir / CHAPTERS).mkdir(parents=True)
        (story_dir / CHAPTERS / KEEP).touch()
        write_manifest(
            story_dir / MANIFEST,
            {"title": name, "synopsis": summary, CHAPTERS: []},
        )

        self.invalidate()
        return self.story(derive_id(SPACE, f"{STORIES}/{story_dir.name}"))

    def update_story(
        self, story_id: str, *, name: str | None = None, summary: str | None = None
    ) -> Story:
        """Retitles a story, or rewrites its synopsis. The directory keeps its slug.

        The id is derived from the path, so it does not change here: a story can be
        renamed without every chapter id under it moving.
        """
        story = self.story(story_id)
        manifest = self.path(story.relpath / MANIFEST)

        document = read_manifest(manifest)
        if name is not None:
            document["title"] = name
        if summary is not None:
            document["synopsis"] = summary
        write_manifest(manifest, document)

        self.invalidate()
        return self.story(story_id)

    def delete_story(self, story_id: str, *, force: bool = False) -> None:
        """Deletes a story directory and the chapters in it.

        Refused while the directory holds anything else. A lorebook and a timeline are
        written by hand and are not this endpoint's to remove — unless `force` is set,
        which deletes the directory whole regardless of what is in it.
        """
        story = self.story(story_id)
        story_dir = self.path(story.relpath)

        if not force:
            extra = sorted(
                entry.name
                for entry in story_dir.iterdir()
                if entry.name not in (MANIFEST, CHAPTERS)
            )
            if extra:
                raise Conflict(
                    f'"{story.name}" also holds {", ".join(extra)}, which deleting it '
                    "would remove. Delete those first.",
                    holds=extra,
                )

        shutil.rmtree(story_dir)
        self.invalidate()

    def create_chapter(self, story_id: str, name: str) -> Chapter:
        """Creates an `.ink` file with no prose in a story's chapters directory.

        The file is named after a slug of `name`. Where the slug does not spell the
        name back — capitals, punctuation, accents — the file's header records the name
        as its title, so the writer sees what they typed. A name already shaped like
        its own filename produces a file with no header at all.
        """
        story = self.story(story_id)
        chapters_dir = self.path(story.relpath / CHAPTERS)
        chapters_dir.mkdir(parents=True, exist_ok=True)

        file = free_path(chapters_dir, slugify(name, "chapter"), CHAPTER_SUFFIX)
        header = {"title": name} if name != file.stem else {}
        write_atomically(file, ink.render(header, ""))
        with self._chapter_list(story) as listed:
            ensure_entry(listed, file.name)

        self.invalidate()
        return self.chapter(
            derive_id(SPACE, f"{story.relpath}/{CHAPTERS}/{file.name}")
        )

    def create_one_shot(self, name: str) -> OneShot:
        """Creates an `.ink` file with no prose loose at the stories root.

        Mirrors `create_chapter` minus the story it would otherwise belong to: there
        is no `chapters/` directory to make and no manifest entry to add.
        """
        self._require_root()
        self.stories_dir.mkdir(parents=True, exist_ok=True)

        file = free_path(self.stories_dir, slugify(name, "story"), CHAPTER_SUFFIX)
        header = {"title": name} if name != file.stem else {}
        write_atomically(file, ink.render(header, ""))

        self.invalidate()
        return self.one_shot(derive_id(SPACE, f"{STORIES}/{file.name}"))

    def update_chapter(
        self,
        chapter_id: str,
        *,
        name: str | None = None,
        story_id: str | None = None,
        status: str | None = None,
        summary: str | None = None,
    ) -> Chapter:
        """Renames a chapter, moves it to another story, rewrites what its header says
        about it, or any combination of those.

        The file takes a slug of the new name and its header takes the name itself, so
        what the writer typed is kept whatever the slug drops. The manifest entry
        naming the old file is pointed at the new one, so a chapter that has a place in
        the order keeps it. A chapter moved to another story is listed last there.

        `status` and `summary` are written into the header, and either given as an empty
        string removes the key. Every field given is written in one pass, so a rename and
        a status change in one request rewrite the file once.

        The chapter's id changes whenever its path does, which a rename or a move both
        do. The caller reads the returned chapter for the new one.
        """
        chapter = self.chapter(chapter_id)
        source_story = self.story(chapter.story_id)
        target_story = self.story(story_id) if story_id is not None else source_story
        display = name if name is not None else chapter.name

        source = self.path(chapter.relpath)
        target_dir = self.path(target_story.relpath / CHAPTERS)
        target_dir.mkdir(parents=True, exist_ok=True)

        stem = slugify(name, "chapter") if name is not None else source.stem
        if target_dir == source.parent and stem == source.stem:
            # The new name slugs to the file's own name, so only the recorded title
            # can change. Renaming here would take the file to <stem>-2.ink.
            target = source
        else:
            target = free_path(target_dir, stem, CHAPTER_SUFFIX)
            source.rename(target)

        target_relpath = target_story.relpath / CHAPTERS / target.name
        fields = ink.header_fields(
            title=display if name is not None else None,
            status=status,
            summary=summary,
        )
        if fields:
            self._write_header(target_relpath, fields)

        if target_story.id == source_story.id:
            with self._chapter_list(source_story) as listed:
                entry = entry_for(listed, chapter.filename)
                if entry is not None:
                    entry["file"] = target.name
        else:
            with self._chapter_list(source_story) as listed:
                _remove_entry(listed, chapter.filename)
            with self._chapter_list(target_story) as listed:
                ensure_entry(listed, target.name)

        self.invalidate()
        return self.chapter(derive_id(SPACE, str(target_relpath)))

    def _write_header(self, relpath: PurePosixPath, fields: dict[str, str]) -> None:
        """Applies `fields` to a chapter's header, leaving its prose as it is."""
        text = ink.with_header(ink.parse(self._text(relpath)), relpath.stem, fields)
        if text is not None:
            write_atomically(self.path(relpath), text)

    def update_one_shot(
        self,
        one_shot_id: str,
        *,
        name: str | None = None,
        status: str | None = None,
        summary: str | None = None,
    ) -> OneShot:
        """Renames a one-shot, rewrites what its header says about it, or both.

        Mirrors `update_chapter` minus everything that exists only because a chapter
        belongs to a story: there is no manifest entry to move and nowhere else for a
        one-shot's file to go. The id changes whenever the rename does.
        """
        one_shot = self.one_shot(one_shot_id)
        source = self.path(one_shot.relpath)

        stem = slugify(name, "story") if name is not None else source.stem
        if stem == source.stem:
            target = source
        else:
            target = free_path(self.stories_dir, stem, CHAPTER_SUFFIX)
            source.rename(target)

        target_relpath = PurePosixPath(STORIES) / target.name
        fields = ink.header_fields(
            title=name if name is not None else None, status=status, summary=summary
        )
        if fields:
            self._write_header(target_relpath, fields)

        self.invalidate()
        return self.one_shot(derive_id(SPACE, str(target_relpath)))

    def reorder_chapters(self, story_id: str, order: list[str]) -> Story:
        """Writes `order` into `story.yaml` as the order the story's chapters are read in.

        `order` is chapter ids, and every one of them has to be a chapter of this story:
        a chapter of another, a repeated id and an unknown id are each `Malformed`. A
        chapter of the story that `order` leaves out keeps its place after the ones it
        names, so a client working from a listing taken before someone else added a
        chapter does not drop that chapter out of the manifest.

        Only `story.yaml` is written. No chapter file is read or written.
        """
        story = self.story(story_id)
        own = {chapter.id for chapter in story.files}

        filenames: list[str] = []
        for chapter_id in order:
            if chapter_id not in own:
                raise Malformed(
                    f'"{chapter_id}" is not a chapter of "{story.name}".'
                )
            chapter = self.chapter(chapter_id)
            if chapter.filename in filenames:
                raise Malformed(f'"{chapter_id}" is named twice in the order.')
            filenames.append(chapter.filename)

        with self._chapter_list(story) as listed:
            for filename in filenames:
                ensure_entry(listed, filename)
            named = [entry_for(listed, filename) for filename in filenames]
            # By identity rather than by equality: two entries can be equal mappings,
            # and dropping the wrong one would lose a chapter from the order.
            chosen = {id(entry) for entry in named}
            rest = [entry for entry in listed if id(entry) not in chosen]
            listed[:] = [entry for entry in named if entry is not None] + rest

        self.invalidate()
        return self.story(story_id)

    def delete_chapter(self, chapter_id: str) -> None:
        """Deletes a chapter file, and the manifest entry naming it."""
        chapter = self.chapter(chapter_id)
        story = self.story(chapter.story_id)

        self.path(chapter.relpath).unlink(missing_ok=True)
        with self._chapter_list(story) as listed:
            _remove_entry(listed, chapter.filename)
        self.invalidate()

    def delete_one_shot(self, one_shot_id: str) -> None:
        """Deletes a one-shot's file. There is no manifest entry to remove."""
        one_shot = self.one_shot(one_shot_id)
        self.path(one_shot.relpath).unlink(missing_ok=True)
        self.invalidate()

    @contextmanager
    def _chapter_list(self, story: Story) -> Iterator[list]:
        """Yields the manifest's chapter entries for editing, writing it back if they change.

        Entries are edited in place, so a `status` or any other key the writer put on
        one survives an edit that only touches `file` or `title`.
        """
        manifest = self.path(story.relpath / MANIFEST)
        document = read_manifest(manifest)
        listed = chapter_list(document)
        before = copy.deepcopy(listed)

        yield listed

        if listed != before:
            document[CHAPTERS] = listed
            write_manifest(manifest, document)

    # --- content -----------------------------------------------------------

    def _text(self, relpath: PurePosixPath) -> str:
        """A chapter's text as it is on disk, header and all."""
        return read_text(self.path(relpath), relpath)

    def read_file(self, file_id: str) -> str:
        """A chapter's or a one-shot's prose, without the header above it."""
        file = self.file(file_id)
        return ink.parse(self._text(file.relpath)).body

    def write_file(self, file_id: str, body: str) -> None:
        """Replaces a chapter's or a one-shot's prose, keeping the header the file has.

        The header is read from disk at the moment of the write, not taken from
        anything the client sent, so a title or a status changed by hand since the
        client loaded the file survives the save.
        """
        file = self.file(file_id)
        path = self.path(file.relpath)
        if not path.is_file():
            raise NotFound(f'"{file.relpath}" is no longer on disk.')

        document = ink.parse(self._text(file.relpath))
        write_atomically(path, ink.render(document.metadata, body))
        self._held.restamp()
