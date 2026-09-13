# -*- coding: utf-8 -*-
"""The `.ink` file: prose, with a YAML header carrying what the prose cannot say.

A file may begin with front matter — a YAML mapping between two `---` lines — and the
rest of the file is the prose itself:

    ---
    title: The Letter in the Study
    status: draft
    summary: |
      She finally opens it, and it is not what she was told it was.
    ---
    She had not opened it. Three years of not opening it, and the wax still held.

All three keys are optional, and so is the header. `title` is the name the file is
shown under, which is why a file can be renamed without being moved. `status` is a
free string; the vocabulary suggested for it is `outline`, `draft`, `revised` and
`done`. `summary` says what happens in the file. A key this module does not know is
kept as it is, so a writer can add one and the application will not remove it.

Reading is deliberately forgiving: a header that is unterminated, unparseable or not a
mapping leaves the file with no metadata and all of its text as prose. A file must
never disappear from the tree because its first lines are malformed. `check` is where
those problems are reported instead.

The grammar is only about the header. Nothing here constrains the prose.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .fs import dump_yaml

logger = logging.getLogger(__name__)

#: What one of these files is called.
SUFFIX = ".ink"

#: The line that opens the front matter, and the line that closes it.
FENCE = "---"

#: The keys this module reads. Any other is kept but not understood.
KNOWN_KEYS = ("title", "status", "summary")

#: How much of a file is read when only the header is wanted, so that listing a tree
#: does not load every chapter in it.
MAX_HEADER_BYTES = 64 * 1024

ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Document:
    """One parsed `.ink` file."""

    #: The front matter. Empty when there is none, or when it could not be read.
    metadata: dict = field(default_factory=dict)
    body: str = ""


@dataclass(frozen=True)
class Problem:
    """Something `check` found, at the line it is on."""

    line: int
    level: str
    message: str


def _is_fence(line: str) -> bool:
    """Whether a line is a fence. Trailing whitespace and a `\\r` are allowed on it."""
    return line.rstrip() == FENCE


def _split(text: str) -> tuple[str | None, str]:
    """The front matter and the body, as text.

    The front matter is `None` when the file does not open with a fence, and when it
    opens with one that is never closed — an unclosed fence is a broken header, not a
    header running to the end of the file.

    The body is what follows the closing fence, character for character. Splitting on
    kept line endings and joining them back leaves `\\n` and `\\r\\n` files alike
    exactly as they were.
    """
    lines = text.splitlines(keepends=True)
    if not lines or not _is_fence(lines[0]):
        return None, text

    for index in range(1, len(lines)):
        if _is_fence(lines[index]):
            return "".join(lines[1:index]), "".join(lines[index + 1 :])
    return None, text


def parse(text: str) -> Document:
    """`text` as a header and a body, with no metadata where the header cannot be read."""
    header, body = _split(text)
    if header is None:
        return Document(metadata={}, body=text)

    try:
        document = yaml.safe_load(header)
    except yaml.YAMLError as error:
        logger.warning("front matter could not be parsed: %s", error)
        return Document(metadata={}, body=text)

    if not isinstance(document, dict):
        if document is not None:
            logger.warning("front matter is not a mapping: %r", document)
            return Document(metadata={}, body=text)
        return Document(metadata={}, body=body)

    return Document(metadata=document, body=body)


def render(metadata: dict, body: str) -> str:
    """A file's text: `body`, under a header of `metadata` where there is any.

    Empty metadata writes no fences at all, so a file that needs no header does not
    carry an empty one. Keys are written in the order the mapping holds them, so a
    parsed header keeps its own order and anything new goes after it.
    """
    if not metadata:
        return body
    return f"{FENCE}\n{dump_yaml(metadata)}{FENCE}\n{body}"


def with_title(document: Document, name: str, stem: str) -> str | None:
    """`document` written out under `name`, or `None` if it already says that.

    The title is dropped instead where the filename says the name itself, so renaming a
    file to what it is already called leaves it with no header rather than one
    repeating its own name.
    """
    metadata = dict(document.metadata)
    if name != stem:
        metadata["title"] = name
    else:
        metadata.pop("title", None)

    if metadata == document.metadata:
        return None
    return render(metadata, document.body)


def read_header(path: Path) -> dict:
    """The front matter of the file at `path`, without reading all of it.

    A file that cannot be read, or whose header is malformed, has no metadata. The
    caller lists it all the same, under whatever name its filename gives it.
    """
    try:
        with path.open("r", encoding="utf-8") as handle:
            start = handle.read(MAX_HEADER_BYTES)
    except (OSError, UnicodeDecodeError) as error:
        logger.warning("%s could not be read: %s", path, error)
        return {}

    header, _ = _split(start)
    if header is None:
        return {}
    return parse(f"{FENCE}\n{header}{FENCE}\n").metadata


def display_name(metadata: dict, stem: str) -> str:
    """The name a file is shown under: its title, or its filename without the suffix."""
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    return stem


def _key_line(header: str, key: str) -> int:
    """The line `key` is written on, counting the opening fence as line 1."""
    for offset, line in enumerate(header.splitlines(), start=2):
        if line.startswith(f"{key}:") or line.startswith(f"{key} :"):
            return offset
    return 1


def check(text: str) -> list[Problem]:
    """Everything wrong with `text`'s header, worst first for a given line.

    A file with no header has nothing to report. The prose is not examined.
    """
    lines = text.splitlines()
    if not lines or not _is_fence(lines[0]):
        return []

    header, body = _split(text)
    if header is None:
        return [Problem(1, ERROR, "the front matter is opened but never closed")]

    problems: list[Problem] = []
    try:
        document = yaml.safe_load(header)
    except yaml.YAMLError as error:
        mark = getattr(error, "problem_mark", None)
        line = mark.line + 2 if mark is not None else 1
        detail = getattr(error, "problem", None) or "it is not YAML"
        return [Problem(line, ERROR, f"the front matter is not valid YAML: {detail}")]

    if document is None:
        return problems

    if not isinstance(document, dict):
        return [
            Problem(
                2,
                ERROR,
                "the front matter has to be a mapping of keys to values (this is "
                f"{type(document).__name__})",
            )
        ]

    for key, value in document.items():
        line = _key_line(header, str(key))
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

    body_lines = body.splitlines()
    if body_lines and _is_fence(body_lines[0]):
        problems.append(
            Problem(
                len(header.splitlines()) + 3,
                WARNING,
                "the prose opens with a fence, which reads as a second header",
            )
        )

    problems.sort(key=lambda problem: (problem.line, problem.level))
    return problems
