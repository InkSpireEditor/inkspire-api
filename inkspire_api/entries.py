# -*- coding: utf-8 -*-
"""The two shapes a scan hands back: a file, and a folder that holds files.

Shared by both roots, because a chapter, a one-shot and a note are all exactly a
`File`, and a notes folder and a story are all exactly a `Folder` -- each built from
the same header-reading and manifest-reading calls no matter which root's scan does
it. What differs between them is what each holds, if anything, and how:

- A `Chapter` always belongs to a story. A `Note` may belong to a folder or sit at the
  root. A one-shot belongs to nothing, ever, so it is a bare `File` -- not a sibling
  type, just a `File` with no containment field to give it one.
- A `Story` is a `Folder` that also carries the two booleans saying which further
  views it can offer. A notes folder is a bare `Folder` -- nothing to add.

Each root's scanner stays its own: walking `stories/` and walking the notes root are
different enough -- different manifest filename, different ordering rule, different
git treatment -- that sharing one scanner would cost more than the field lists shared
here save. This module holds only the shapes a scan reads into, not how either scan
gets there -- plus `StoredDocument`, which is what one read of a file answers on either
root, so one route can serve both.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import NamedTuple


class StoredDocument(NamedTuple):
    """One file as it is on disk: where it is, its prose, and its provenance and
    context-summary sections.

    Where it is comes back because recovering stale provenance needs the file's history
    (§7.5), and only `repository.py` may ask git for that — so a scanner hands out what
    to ask about rather than growing a git dependency of its own.

    `provenance` and `context_summary` are each a section's text, unparsed. `ink.py`
    carries a section it does not read, and a scanner is no different.
    """

    relpath: PurePosixPath
    body: str
    provenance: str | None
    context_summary: str | None = None


@dataclass(frozen=True)
class File:
    """One `.ink` file, wherever a scan found it."""

    id: str
    #: Relative to whichever root the scan that found it walked.
    relpath: PurePosixPath
    #: The header's title, or the filename without its suffix.
    name: str
    status: str
    summary: str

    @property
    def filename(self) -> str:
        """The file's own name."""
        return self.relpath.name


@dataclass(frozen=True)
class Folder:
    """One directory, with the files found in it."""

    id: str
    slug: str
    #: Relative to whichever root the scan that found it walked.
    relpath: PurePosixPath
    name: str
    summary: str
    files: tuple[File, ...]
