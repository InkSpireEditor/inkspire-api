# -*- coding: utf-8 -*-
"""Paths, ids and YAML, for every tree the API serves.

The API serves two trees from two roots: the stories in the data repository, and the
files that belong to no story. Both address what they hold by an id derived from a
path, name files after a slug of what the writer typed, and keep their metadata in
YAML. That is what lives here; what a story is lives in `storage.py`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import yaml

logger = logging.getLogger(__name__)

#: Hex characters kept from the digest, so 64 bits of it.
ID_LENGTH = 16

#: Longest accepted name of anything: a story, a chapter, a folder, a file.
MAX_NAME_LENGTH = 255

#: Longest accepted synopsis or context.
MAX_SUMMARY_LENGTH = 2000

#: Largest file accepted from a client. Prose reaches nowhere near this; the cap is
#: here so a runaway request cannot be read into memory whole.
MAX_FILE_BYTES = 4 * 1024 * 1024


class StorageError(RuntimeError):
    """Something on disk is not as the API needs it."""


class NotFound(StorageError):
    """Nothing in either tree has that id."""


class Conflict(StorageError):
    """The operation would destroy or overwrite something."""


class Malformed(StorageError):
    """An authored file is there and cannot be read as what it claims to be.

    Not a client error and not a bug: a person wrote the file and has to fix it, so
    the message says what is wrong with it rather than being swallowed.
    """


def derive_id(space: str, relpath: str) -> str:
    """The id of a path, relative to the root of the space it is in.

    The space is part of what is hashed, so the same relative path in two roots gets
    two ids. Without it, `stories/notes` in one root and in the other would be one id
    for two different files.
    """
    digest = hashlib.blake2b(f"{space}\0{relpath}".encode("utf-8"))
    return digest.hexdigest()[:ID_LENGTH]


def slugify(name: str, fallback: str) -> str:
    """Turns a name into the filename it is stored under.

    Letters and digits are kept, spaces and underscores become single dashes, and
    everything else is dropped. A name made entirely of dropped characters yields
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


class Dumper(yaml.SafeDumper):
    """Writes a value of several lines as a block, so it stays readable."""


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


Dumper.add_representer(str, _represent_str)


def dump_yaml(document: dict) -> str:
    """`document` as YAML, keys in the order they were inserted.

    Anything YAML does not carry into a parsed document — comments, blank lines,
    quoting style — is not preserved by a read and a write through here.
    """
    return yaml.dump(
        document,
        Dumper=Dumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def read_yaml_file(path: Path) -> dict:
    """The parsed file, or an empty mapping if it cannot be read.

    A manifest hand-edited into something unparseable must not hide what sits beside
    it, so the caller carries on without the metadata and says so in the log.
    """
    try:
        return load_yaml(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        logger.warning("%s could not be read: %s", path, error)
        return {}


def load_yaml(text: str) -> dict:
    """`text` parsed as a YAML mapping, or an empty one if it is anything else.

    Raises `yaml.YAMLError` for text that is not YAML at all. A caller reading a file
    somebody may have hand-edited catches that and carries on without the metadata.
    """
    document = yaml.safe_load(text)
    return document if isinstance(document, dict) else {}


def resolve_within(root: Path, relpath: PurePosixPath, what: str) -> Path:
    """The absolute path of something in a root, refused if it is not in it.

    Resolving follows symlinks, so a link out of the root is caught here as well as by
    the scan that refuses to list one. `what` names the root in the message.
    """
    resolved = (root / relpath).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise StorageError(f'"{relpath}" resolves outside the {what}.')
    return resolved


def read_text(path: Path, relpath: PurePosixPath) -> str:
    """A file's text, with the two ways reading one can fail told apart.

    `relpath` is what the message names, since that is what a client could recognise.
    """
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise NotFound(f'"{relpath}" is no longer on disk.') from None
    except UnicodeDecodeError as error:
        raise StorageError(f'"{relpath}" is not UTF-8 text: {error.reason}.') from error


def mtime(path: Path) -> int | None:
    """When a file last changed, or `None` if it is not there to ask."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def directories(directory: Path) -> list[Path]:
    """The directories in `directory`, in name order. A symlinked one is not listed."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return sorted(
        (entry for entry in entries if entry.is_dir() and not entry.is_symlink()),
        key=lambda entry: entry.name,
    )


def files_with_suffix(directory: Path, suffix: str) -> list[Path]:
    """The files in `directory` ending in `suffix`, in name order, symlinks left out."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return sorted(
        (
            entry
            for entry in entries
            if entry.suffix == suffix and entry.is_file() and not entry.is_symlink()
        ),
        key=lambda entry: entry.name,
    )


class HeldScan[T]:
    """One scan of a root, held until the files it was built from change.

    A scan is kept rather than repeated because it parses YAML and reads a header out
    of every file. `stamp` is what that is checked against: something cheap to compute
    which changes whenever the scan would come out different.

    Sync endpoints run in a thread pool, so two requests can reach this at once. The
    lock covers taking the stamp and rebuilding as one step.
    """

    def __init__(self, build: Callable[[], T], stamp: Callable[[], tuple]) -> None:
        self._build = build
        self._stamp_now = stamp
        self._lock = threading.Lock()
        self._stamp: tuple | None = None
        self._held: T | None = None

    def get(self) -> T:
        """The current scan, rebuilt first if anything it reads has changed."""
        with self._lock:
            stamp = self._stamp_now()
            if self._held is None or stamp != self._stamp:
                self._held = self._build()
                self._stamp = stamp
            return self._held

    def drop(self) -> None:
        """Throws the scan away, after a change this process made."""
        with self._lock:
            self._held = None
            self._stamp = None

    def restamp(self) -> None:
        """Records the files as they are now, keeping the scan.

        A write that changed no name, no path and no header leaves the scan right and
        the stamp it was taken with stale. Restamping keeps the scan rather than
        rebuilding it for prose the scan never reads.
        """
        with self._lock:
            if self._held is not None:
                self._stamp = self._stamp_now()


class HeldPerKey[T]:
    """A `HeldScan` per key, made the first time that key is asked for.

    What `build` produces from one story is expensive enough to keep and small enough
    to hold: a parsed timeline, or a graph of a few hundred triples. Each key gets its
    own held value and its own stamp, so one story's edit rebuilds only that story.

    Nothing is evicted. A key is only ever added by a request naming a story that
    exists, so the number of them is the number of stories.
    """

    def __init__(
        self, build: Callable[[str], T], stamp: Callable[[str], tuple]
    ) -> None:
        self._build = build
        self._stamp = stamp
        self._lock = threading.Lock()
        self._held: dict[str, HeldScan[T]] = {}

    def get(self, key: str) -> T:
        """The value for `key`, rebuilt first if what it was built from has changed."""
        with self._lock:
            held = self._held.get(key)
            if held is None:
                held = HeldScan(lambda: self._build(key), lambda: self._stamp(key))
                self._held[key] = held
        return held.get()
