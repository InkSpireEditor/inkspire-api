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
gets there.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath


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
