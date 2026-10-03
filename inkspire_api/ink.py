# -*- coding: utf-8 -*-
"""The `.ink` file: named sections, one of which is the prose.

A file is a sequence of sections. Each opens with a fence line naming it and runs to
the next fence line or to the end of the file:

    ===== ink:meta
    title: The Letter in the Study
    status: draft
    summary: |
      She finally opens it, and it is not what she was told it was.
    ===== ink:body
    She had not opened it. Three years of not opening it, and the wax still held.
    ===== ink:provenance
    47f57caaa4fb330e: [[18, 45, "gen"]]

A file with no fence line anywhere is all prose — that is what a writer gets by
creating a file and typing in it, and it stays legible to anything that reads text.

`ink:meta` is a YAML mapping and is what this module understands. All of its keys are
optional, and so is the section. `title` is the name the file is shown under, which is
why a file can be renamed without being moved. `status` is a free string; the
vocabulary suggested for it is `outline`, `draft`, `revised` and `done`. `summary` says
what happens in the file. A key this module does not know is kept as it is, so a writer
can add one and the application will not remove it.

`ink:body` is the prose, and is opaque here. `ink:provenance` is per-character
provenance keyed by paragraph hash, and is opaque here too — this module carries it
from disk to the caller and back without reading it, which is what lets the format gain
a section without this file changing.

Two rules the rest of the application depends on:

- **`ink:meta` comes first.** `read_header` reads only the first `MAX_HEADER_BYTES` of a
  file so that listing a tree does not load every chapter in it, so a header further
  down would not be found. `render` always writes it first.
- **A section derived from the body is written with it or not at all.** `render` takes
  all three parts for exactly that reason: it has no signature that can write prose and
  leave a stale hash behind it.

Reading is deliberately forgiving. A file whose sections cannot be made sense of — prose
above the first fence, a `meta` section that is not YAML — keeps all of its text, and
`check` is where the problem is reported instead. A file must never disappear from the
tree because its first lines are malformed.
"""

from __future__ import annotations

import logging
import string
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .fs import dump_yaml

logger = logging.getLogger(__name__)

#: What one of these files is called.
SUFFIX = ".ink"

#: The five characters a fence line opens with. Exactly five, not five or more.
FENCE = "====="

#: What follows the fence, before the section's name.
FENCE_PREFIX = "ink:"

#: The header: a YAML mapping of what the prose cannot say.
SECTION_META = "meta"

#: The prose.
SECTION_BODY = "body"

#: Per-character provenance, keyed by paragraph hash. Opaque to this module.
SECTION_PROVENANCE = "provenance"

#: The section names the format defines. Any other is kept but not understood.
KNOWN_SECTIONS = (SECTION_META, SECTION_BODY, SECTION_PROVENANCE)

#: The header keys this module reads. Any other is kept but not understood.
KNOWN_KEYS = ("title", "status", "summary")

#: The longest a section name may be.
MAX_NAME_LENGTH = 32

#: The characters a section name is made of.
NAME_CHARACTERS = frozenset(string.ascii_lowercase + "_")

#: How much of a file is read when only the header is wanted, so that listing a tree
#: does not load every chapter in it.
MAX_HEADER_BYTES = 64 * 1024

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Document:
    """One parsed `.ink` file."""

    #: The `ink:meta` mapping. Empty when there is none, or when it could not be read.
    metadata: dict = field(default_factory=dict)
    #: The `ink:body` section, or the whole file where there are no sections.
    body: str = ""
    #: Every other section, by name, as text. Opaque here and preserved verbatim.
    sections: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Problem:
    """Something `check` found, at the line it is on."""

    line: int
    level: str
    message: str


@dataclass(frozen=True)
class _Section:
    """One section as it was read: its name, its text, and the line its fence is on."""

    name: str
    text: str
    line: int


def fence_line(name: str) -> str:
    """The line that opens the section called `name`."""
    return f"{FENCE} {FENCE_PREFIX}{name}\n"


def section_name(line: str) -> str | None:
    """The name the fence line `line` opens, or `None` where it is not a fence line.

    Exactly five `=`, then at least one space, then `ink:`, then the name, then only
    spaces before the end of the line. A tab anywhere, a sixth `=`, or a name with a
    character outside `NAME_CHARACTERS` all mean this is an ordinary line of prose.
    """
    if not line.startswith(FENCE):
        return None
    rest = line[len(FENCE) :].rstrip("\n").rstrip("\r")
    if not rest.startswith(" "):
        return None
    rest = rest.lstrip(" ")
    if not rest.startswith(FENCE_PREFIX):
        return None
    name = rest[len(FENCE_PREFIX) :].rstrip(" ")
    if not name or len(name) > MAX_NAME_LENGTH or not set(name) <= NAME_CHARACTERS:
        return None
    return name


def _read(text: str) -> list[_Section] | None:
    """`text`'s sections in the order they appear, or `None` where it has none.

    `None` covers the two cases that are all prose and nothing else: a file with no
    fence line anywhere, and a file with prose above its first fence. The second is
    malformed — `check` reports it — and is read this way because losing a provenance
    section is recoverable (it can be rebuilt from history) while losing prose is not.
    """
    found: list[tuple[str, int, list[str]]] = []
    for number, line in enumerate(text.splitlines(keepends=True), start=1):
        name = section_name(line)
        if name is not None:
            found.append((name, number, []))
        elif found:
            found[-1][2].append(line)
        else:
            return None
    if not found:
        return None
    return [_Section(name, "".join(lines), number) for name, number, lines in found]


def _mapping(header: str) -> dict:
    """`header` as a mapping, or empty where it is not one or is not YAML."""
    try:
        loaded = yaml.safe_load(header)
    except yaml.YAMLError as error:
        logger.warning("the meta section could not be parsed: %s", error)
        return {}
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        logger.warning("the meta section is not a mapping: %r", loaded)
        return {}
    return loaded


def parse(text: str) -> Document:
    """`text` as a header, a body and whatever other sections it carries.

    A file with no sections is all body, with no metadata and no sections — which is
    what the six files written before this format existed are. A section named twice
    keeps its last occurrence, as a YAML mapping would.
    """
    sections = _read(text)
    if sections is None:
        return Document(metadata={}, body=text, sections={})

    held: dict[str, str] = {}
    for section in sections:
        if section.name in held:
            logger.warning(
                'the section "%s" appears more than once; the last one wins', section.name
            )
        held[section.name] = section.text

    header = held.pop(SECTION_META, None)
    body = held.pop(SECTION_BODY, "")
    return Document(metadata=_mapping(header or ""), body=body, sections=held)


def render(metadata: dict, body: str, sections: dict[str, str]) -> str:
    """A file's text: `metadata`, then `body`, then `sections` in the order given.

    All three are required because a section derived from the body may not be written
    without it, nor the body without the section — a signature that defaulted either
    away would make dropping a footer the easy mistake.

    Nothing to record either side of the prose writes no fences at all, so a file that
    needs no metadata is a plain text file. Keys are written in the order the mapping
    holds them, so a parsed header keeps its own order and anything new goes after it.

    A fence line must start a line, so a section with another after it is given a
    closing newline where it has none. That is the only byte a read and a write can
    change, and only once: everything this writes reads back as the same three parts.

    Raises `ValueError` on a body holding a line that would read back as a fence, rather
    than writing a file it could not parse. Escaping it silently would make the prose
    stop being the prose.
    """
    for number, line in enumerate(body.splitlines(), start=1):
        if section_name(line) is not None:
            raise ValueError(
                f"line {number} of the prose reads as a section fence: {line.strip()!r}"
            )

    if not metadata and not sections:
        return body

    parts: list[str] = []
    if metadata:
        parts.append(fence_line(SECTION_META))
        parts.append(dump_yaml(metadata))
    parts.append(fence_line(SECTION_BODY))
    parts.append(body)
    for name, text in sections.items():
        if parts[-1] and not parts[-1].endswith("\n"):
            parts.append("\n")
        parts.append(fence_line(name))
        parts.append(text)
    return "".join(parts)


def header_fields(
    *,
    title: str | None = None,
    status: str | None = None,
    summary: str | None = None,
) -> dict[str, str]:
    """The header fields an update asks to change, leaving out the ones it does not.

    `None` means a field was not mentioned and is left alone; `""` means it was asked for
    as empty, which `with_header` reads as a removal.
    """
    given = {"title": title, "status": status, "summary": summary}
    return {key: value for key, value in given.items() if value is not None}


def with_header(document: Document, stem: str, fields: dict) -> str | None:
    """`document` written out with `fields` applied to its header, or `None` if that
    would write back what is already there.

    A field is removed rather than written where it has nothing to say: a `title` the
    filename already spells, and any field given as an empty string. So renaming a file
    to what it is already called leaves it with no header instead of one repeating its
    own name, and clearing a status removes the key instead of recording `status: ''`.

    Keys `fields` does not mention are left alone, which is what keeps a status that was
    set by hand while a rename is saved.
    """
    metadata = dict(document.metadata)
    for key, value in fields.items():
        if value == "" or (key == "title" and value == stem):
            metadata.pop(key, None)
        else:
            metadata[key] = value

    if metadata == document.metadata:
        return None
    return render(metadata, document.body, document.sections)


def read_header(path: Path) -> dict:
    """The `ink:meta` mapping of the file at `path`, without reading all of it.

    A file that cannot be read, or that does not open with a `meta` section, has no
    metadata. So does one whose `meta` section is longer than the budget, which is told
    from a short file by whether the read stopped at the budget or at the end of the
    file: a `meta` section with nothing after it is the whole file only in the second
    case. The caller lists the file all the same, under whatever name its filename gives.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            start = handle.read(MAX_HEADER_BYTES)
    except (OSError, UnicodeDecodeError) as error:
        logger.warning("%s could not be read: %s", path, error)
        return {}

    sections = _read(start)
    if not sections or sections[0].name != SECTION_META:
        return {}
    if len(sections) < 2 and len(start) == MAX_HEADER_BYTES:
        return {}
    return _mapping(sections[0].text)


def display_name(metadata: dict, stem: str) -> str:
    """The name a file is shown under: its title, or its filename without the suffix."""
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return stem


def _key_line(section: _Section, key: str) -> int:
    """The line `key` is written on, counting the section's fence as `section.line`."""
    for offset, line in enumerate(section.text.splitlines(), start=section.line + 1):
        if line.startswith(f"{key}:") or line.startswith(f"{key} :"):
            return offset
    return section.line


def _header_problems(section: _Section) -> list[Problem]:
    """Everything wrong inside a `meta` section."""
    try:
        loaded = yaml.safe_load(section.text)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = mark.line + section.line + 1 if mark is not None else section.line
        detail = getattr(error, "problem", None) or "it is not YAML"
        return [Problem(line, ERROR, f"the meta section is not valid YAML: {detail}")]

    if loaded is None:
        return []

    if not isinstance(loaded, dict):
        return [
            Problem(
                section.line + 1,
                ERROR,
                "the meta section has to be a mapping of keys to values (this is "
                f"{type(loaded).__name__})",
            )
        ]

    problems: list[Problem] = []
    for key, value in loaded.items():
        line = _key_line(section, str(key))
        if key not in KNOWN_KEYS:
            problems.append(Problem(line, WARNING, f'the key "{key}" means nothing here'))
        elif not isinstance(value, str):
            problems.append(
                Problem(
                    line,
                    ERROR,
                    f'"{key}" has to be text (this is {type(value).__name__})',
                )
            )
        elif key == "title" and "\n" in value:
            problems.append(Problem(line, ERROR, "a title cannot run over two lines"))
    return problems


def check(text: str) -> list[Problem]:
    """Everything wrong with `text`'s sections, worst first for a given line.

    A file with no sections has nothing to report: it is all prose, and the prose is
    never examined.
    """
    sections = _read(text)
    if sections is None:
        if any(section_name(line) is not None for line in text.splitlines()):
            return [Problem(1, ERROR, "the file has prose above its first section")]
        return []

    problems: list[Problem] = []
    seen: set[str] = set()
    for index, section in enumerate(sections):
        if section.name in seen:
            problems.append(
                Problem(
                    section.line,
                    ERROR,
                    f'the section "{section.name}" is opened more than once',
                )
            )
        seen.add(section.name)

        if section.name == SECTION_META:
            if index != 0:
                problems.append(
                    Problem(
                        section.line,
                        ERROR,
                        "the meta section has to come first, or a tree listing will "
                        "not read it",
                    )
                )
            problems.extend(_header_problems(section))
        elif section.name not in KNOWN_SECTIONS:
            problems.append(
                Problem(
                    section.line,
                    WARNING,
                    f'the section "{section.name}" means nothing here',
                )
            )

    problems.sort(key=lambda problem: (problem.line, problem.level))
    return problems
