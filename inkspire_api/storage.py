# -*- coding: utf-8 -*-
"""The stories on disk, and the ids the API addresses them by.

The layout is one directory per story, each holding a `story.yaml` and a `chapters/`
directory of `.ink` files:

    stories/<story-slug>/story.yaml
    stories/<story-slug>/chapters/<chapter-slug>.ink

A directory is a story if and only if it holds a `story.yaml`. That file gives the
story its title and synopsis; the filesystem determines which chapters exist. Anything
else in a story directory — a `lorebook/`, a `timeline.yaml` — is not a chapter and is
not listed.

A client names a story or a chapter by an id derived from its path, never by the path,
so no request can point at a location on disk. Ids need no table and survive a restart.
Renaming a chapter changes its id, because it is then a different path.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
import shutil
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

logger = logging.getLogger(__name__)

STORIES = "stories"
CHAPTERS = "chapters"
MANIFEST = "story.yaml"
CHAPTER_SUFFIX = ".ink"

#: Hex characters kept from the digest, so 64 bits of it.
ID_LENGTH = 16

#: Longest accepted story title or chapter name.
MAX_NAME_LENGTH = 255

#: Longest accepted synopsis.
MAX_SUMMARY_LENGTH = 2000

#: Largest chapter accepted from a client. Prose reaches nowhere near this; the cap is
#: here so a runaway request cannot be read into memory whole.
MAX_CHAPTER_BYTES = 4 * 1024 * 1024

#: Placed in a chapters directory that has none yet, because git stores no empty
#: directory and the story would arrive at a clone without one.
KEEP = ".gitkeep"


class StorageError(RuntimeError):
    """Something on disk is not as the API needs it."""


class DataRootMissing(StorageError):
    """The story repository is not present where it is configured to be."""


class NotFound(StorageError):
    """No story or chapter has that id."""


class Conflict(StorageError):
    """The operation would destroy or overwrite something."""


def derive_id(relpath: str) -> str:
    """The id of a repository-relative path."""
    return hashlib.blake2b(relpath.encode("utf-8")).hexdigest()[:ID_LENGTH]


def slugify(name: str, fallback: str) -> str:
    """Turns a title into the filename it is stored under.

    Letters and digits are kept, spaces and underscores become single dashes, and
    everything else is dropped. A title made entirely of dropped characters yields
    `fallback`, since a file still needs a name.
    """
    slug = re.sub(r"[^\w\s-]", "", name.lower())
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or fallback


def free_path(directory: Path, stem: str, suffix: str = "") -> Path:
    """A path in `directory` that nothing occupies, numbering from `-2` if needed.

    Two stories may share a title and two chapters a name; they cannot share a path.
    """
    candidate = directory / f"{stem}{suffix}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{suffix}"
        counter += 1
    return candidate


@dataclass(frozen=True)
class Chapter:
    id: str
    #: Relative to the repository root.
    relpath: PurePosixPath
    name: str
    story_id: str

    @property
    def filename(self) -> str:
        return self.relpath.name


@dataclass(frozen=True)
class Story:
    id: str
    slug: str
    relpath: PurePosixPath
    name: str
    summary: str
    chapters: tuple[Chapter, ...]


@dataclass(frozen=True)
class Tree:
    """One scan of the repository, indexed by id."""

    stories: dict[str, Story]
    chapters: dict[str, Chapter]


def read_manifest(path: Path) -> dict:
    """The parsed `story.yaml`, or an empty mapping if it cannot be read.

    A file that has been hand-edited into something unparseable must not hide the
    chapters beside it, so the story keeps its slug as a name and stays reachable.
    """
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        logger.warning("%s could not be read: %s", path, error)
        return {}
    return document if isinstance(document, dict) else {}


class _Dumper(yaml.SafeDumper):
    """Writes a synopsis of several lines as a block, so it stays readable."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _represent_str)


def write_manifest(path: Path, document: dict) -> None:
    """Replaces `story.yaml` with `document`, keys in the order they were inserted.

    The file is rewritten from the parsed document, so anything YAML does not carry
    into that — comments, blank lines, quoting style — is not preserved.
    """
    text = yaml.dump(
        document,
        Dumper=_Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    write_atomically(path, text)


def write_atomically(path: Path, text: str) -> None:
    """Writes `text` to `path` through a temporary file in the same directory.

    A write interrupted part way leaves the previous content in place instead of a
    truncated file, which for a chapter is the difference between a lost save and a
    lost afternoon.
    """
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def chapter_list(document: dict) -> list:
    """The manifest's chapter entries. An absent or malformed list reads as empty."""
    listed = document.get(CHAPTERS)
    return listed if isinstance(listed, list) else []


def manifest_titles(document: dict) -> dict[str, str]:
    """Maps a chapter filename to the title `story.yaml` gives it, where it gives one."""
    return {
        str(entry["file"]): str(entry["title"])
        for entry in chapter_list(document)
        if isinstance(entry, dict) and entry.get("file") and entry.get("title")
    }


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


def record_chapter(listed: list, filename: str, name: str) -> None:
    """Makes the manifest show `filename` under `name`.

    A title is recorded only where the filename does not already say it, so a chapter
    called after its own slug adds nothing to the file. An entry is appended when none
    names the file yet, which also places the chapter last in the order.
    """
    entry = entry_for(listed, filename)
    titled = name != filename.removesuffix(CHAPTER_SUFFIX)

    if entry is None:
        if titled:
            listed.append({"file": filename, "title": name})
    elif titled:
        entry["title"] = name
    else:
        entry.pop("title", None)


class Scanner:
    """Reads the repository, and makes the changes the API is asked for.

    A scan walks `stories/`, parses each `story.yaml` and lists each `chapters/`. The
    result is held until the files or a manifest change, so repeated requests do not
    reparse unchanged YAML. Writing a chapter's content leaves the held scan valid: it
    changes no name and no path.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        # Sync endpoints run in a thread pool, so two requests can reach the cache at
        # once. The lock covers reading the stamp and rebuilding as one step.
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._tree: Tree | None = None

    # --- reading -----------------------------------------------------------

    @property
    def stories_dir(self) -> Path:
        return self.root / STORIES

    def tree(self) -> Tree:
        with self._lock:
            stamp = self._current_stamp()
            if self._tree is None or stamp != self._stamp:
                self._tree = self._scan()
                self._stamp = stamp
            return self._tree

    def invalidate(self) -> None:
        """Drops the held scan, after a change this process made."""
        with self._lock:
            self._tree = None
            self._stamp = None

    def story(self, story_id: str) -> Story:
        story = self.tree().stories.get(story_id)
        if story is None:
            raise NotFound(f'No story with id "{story_id}".')
        return story

    def chapter(self, chapter_id: str) -> Chapter:
        chapter = self.tree().chapters.get(chapter_id)
        if chapter is None:
            raise NotFound(f'No chapter with id "{chapter_id}".')
        return chapter

    def path(self, relpath: PurePosixPath) -> Path:
        """The absolute path of something in the repository.

        Resolving follows symlinks, so a link out of the repository is caught here as
        well as by the scan that refuses to list one.
        """
        resolved = (self.root / relpath).resolve()
        if not resolved.is_relative_to(self.root.resolve()):
            raise StorageError(f'"{relpath}" resolves outside the story repository.')
        return resolved

    def _story_dirs(self) -> list[Path]:
        """The story directories, in name order. A symlinked directory is not one."""
        try:
            entries = list(self.stories_dir.iterdir())
        except FileNotFoundError:
            return []
        return sorted(
            (entry for entry in entries if entry.is_dir() and not entry.is_symlink()),
            key=lambda entry: entry.name,
        )

    @staticmethod
    def _mtime(path: Path) -> int | None:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return None

    def _current_stamp(self) -> tuple:
        """What the held scan is checked against: which files exist, and when each
        manifest last changed.

        Listing the directories rather than trusting their mtimes is deliberate. A
        filesystem timestamp advances in ticks of about a millisecond, so a chapter
        written within one tick of the previous scan would leave the mtime it was
        scanned at and never be noticed.

        A manifest is compared by mtime, since reading each one is the work the held
        scan exists to avoid. Rewriting one within a tick of a scan is therefore
        missed; a change made through this class invalidates the scan outright.
        """
        return tuple(
            (
                story_dir.name,
                self._mtime(story_dir / MANIFEST),
                tuple(file.name for file in self._chapter_files(story_dir / CHAPTERS)),
            )
            for story_dir in self._story_dirs()
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
            story_id = derive_id(str(relpath))
            document = read_manifest(manifest)
            titles = manifest_titles(document)

            own: list[Chapter] = []
            for file in self._chapter_files(story_dir / CHAPTERS):
                chapter_relpath = relpath / CHAPTERS / file.name
                chapter = Chapter(
                    id=derive_id(str(chapter_relpath)),
                    relpath=chapter_relpath,
                    name=titles.get(file.name, file.stem),
                    story_id=story_id,
                )
                own.append(chapter)
                chapters[chapter.id] = chapter

            own.sort(key=lambda chapter: chapter.name)
            stories[story_id] = Story(
                id=story_id,
                slug=slug,
                relpath=relpath,
                name=str(document.get("title") or slug),
                summary=str(document.get("synopsis") or ""),
                chapters=tuple(own),
            )

        return Tree(stories=stories, chapters=chapters)

    @staticmethod
    def _chapter_files(chapters_dir: Path) -> list[Path]:
        try:
            entries = list(chapters_dir.iterdir())
        except OSError:
            return []
        return sorted(
            (
                entry
                for entry in entries
                if entry.suffix == CHAPTER_SUFFIX
                and entry.is_file()
                and not entry.is_symlink()
            ),
            key=lambda entry: entry.name,
        )

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
        return self.story(derive_id(f"{STORIES}/{story_dir.name}"))

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

    def delete_story(self, story_id: str) -> None:
        """Deletes a story directory and the chapters in it.

        Refused while the directory holds anything else. A lorebook and a timeline are
        written by hand and are not this endpoint's to remove.
        """
        story = self.story(story_id)
        story_dir = self.path(story.relpath)

        extra = sorted(
            entry.name
            for entry in story_dir.iterdir()
            if entry.name not in (MANIFEST, CHAPTERS)
        )
        if extra:
            raise Conflict(
                f'"{story.name}" also holds {", ".join(extra)}, which deleting it '
                "would remove. Delete those first."
            )

        shutil.rmtree(story_dir)
        self.invalidate()

    def create_chapter(self, story_id: str, name: str) -> Chapter:
        """Creates an empty `.ink` file in a story's chapters directory.

        The file is named after a slug of `name`. Where the slug does not spell the
        name back — capitals, punctuation, accents — the manifest records the name as
        the chapter's title, so the writer sees what they typed.
        """
        story = self.story(story_id)
        chapters_dir = self.path(story.relpath / CHAPTERS)
        chapters_dir.mkdir(parents=True, exist_ok=True)

        file = free_path(chapters_dir, slugify(name, "chapter"), CHAPTER_SUFFIX)
        file.touch()
        with self._chapter_list(story) as listed:
            record_chapter(listed, file.name, name)

        self.invalidate()
        return self.chapter(derive_id(f"{story.relpath}/{CHAPTERS}/{file.name}"))

    def update_chapter(
        self, chapter_id: str, *, name: str | None = None, story_id: str | None = None
    ) -> Chapter:
        """Renames a chapter, moves it to another story, or both.

        The file takes a slug of the new name, and the manifest entry naming the old
        file is pointed at the new one, so a chapter that has a place in the order
        keeps it. Moving a chapter to another story leaves it unordered there, shown
        after the chapters that story lists.

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

        if target_story.id == source_story.id:
            with self._chapter_list(source_story) as listed:
                entry = entry_for(listed, chapter.filename)
                if entry is not None:
                    entry["file"] = target.name
                record_chapter(listed, target.name, display)
        else:
            with self._chapter_list(source_story) as listed:
                _remove_entry(listed, chapter.filename)
            with self._chapter_list(target_story) as listed:
                record_chapter(listed, target.name, display)

        self.invalidate()
        return self.chapter(
            derive_id(f"{target_story.relpath}/{CHAPTERS}/{target.name}")
        )

    def delete_chapter(self, chapter_id: str) -> None:
        """Deletes a chapter file, and the manifest entry naming it."""
        chapter = self.chapter(chapter_id)
        story = self.story(chapter.story_id)

        self.path(chapter.relpath).unlink(missing_ok=True)
        with self._chapter_list(story) as listed:
            _remove_entry(listed, chapter.filename)
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

    def read_chapter(self, chapter_id: str) -> str:
        chapter = self.chapter(chapter_id)
        try:
            return self.path(chapter.relpath).read_text(encoding="utf-8")
        except FileNotFoundError:
            raise NotFound(f'"{chapter.relpath}" is no longer on disk.') from None
        except UnicodeDecodeError as error:
            raise StorageError(
                f'"{chapter.relpath}" is not UTF-8 text: {error.reason}.'
            ) from error

    def write_chapter(self, chapter_id: str, text: str) -> None:
        chapter = self.chapter(chapter_id)
        path = self.path(chapter.relpath)
        if not path.is_file():
            raise NotFound(f'"{chapter.relpath}" is no longer on disk.')
        write_atomically(path, text)
