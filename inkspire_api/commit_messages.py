# -*- coding: utf-8 -*-
"""Proposing a commit message for what is staged.

    POST /api/git/commit-message

`POST /api/git/commit` commits the index, so the proposal describes the staged changes:
each staged path, labelled as the writer knows it (its story, its place in the story's
chapter order, its title), and the diff of its text between `HEAD` and the index. Its own
template (`templates/commit_message.j2`) asks for one line, `<Kind>: <what>`, about the
files rather than the story's events. Answered in one response through
`LLMService.complete`, from `INKSPIRE_LLM_SMALL_MODEL`, as a title is (`titles.py`).

Only the body of an `.ink` file is compared: every edited paragraph changes its hash in
`ink:provenance`, and a save can rewrite `ink:context_summary`, so a raw diff of the file
would be mostly hashes. A `story.yaml` is compared as it is. Any other staged path is
listed by name only.

Reads the repository and takes no git lock. The index can change between the proposal and
the commit; the commit takes the index as it then is.
"""

from __future__ import annotations

import dataclasses
import difflib
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Request, status
from git import Repo
from git.exc import GitCommandError
from pydantic import BaseModel, Field

from . import ink
from . import prompt as prompt_lib
from .activity import body_of
from .deps import CurrentUser, SettingsDep
from .files import ScannerDep
from .fs import derive_id
from .llm import LLMError, RefusedProvider, ServiceDep, UnknownModel
from .repository import GitRefused, NameStatus, RepoDep, parse_name_status
from .storage import CHAPTER_SUFFIX, CHAPTERS, MANIFEST, SPACE, STORIES, Scanner, Tree

router = APIRouter(prefix="/git", tags=["git"])

COMMIT_MESSAGE_TEMPLATE = "commit_message.j2"

#: Ceiling on a proposed message, and on the previous one sent back to ask for another.
#: One line naming a few files; `fs.MAX_COMMIT_MESSAGE_LENGTH` is the much larger ceiling
#: on a message the writer commits.
MAX_PROPOSAL_LENGTH = 120

NOTHING_STAGED = "Nothing is staged."
NO_SMALL_MODEL = (
    "No small model is configured. Set INKSPIRE_LLM_SMALL_MODEL to a model this "
    "feature may call."
)
NOTHING_USABLE = "The model answered with nothing usable as a commit message."

#: The word each `--name-status` letter is written as in the list the model reads.
STATES = {
    "A": "added",
    "M": "modified",
    "T": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
}


class CommitMessageRequest(BaseModel):
    """`previous` is the last proposal, sent by the dice button to ask for another one."""

    previous: str | None = Field(default=None, max_length=MAX_PROPOSAL_LENGTH)


@dataclasses.dataclass(frozen=True)
class StagedFile:
    """One staged path as the model reads it.

    `old_path` is set for a rename or a copy. `diff` is `""` for a deleted file, for a
    path that is neither an `.ink` file nor a `story.yaml`, and for one whose diff did
    not fit the budget.
    """

    path: str
    state: str
    label: str
    old_path: str | None = None
    diff: str = ""


@dataclasses.dataclass(frozen=True)
class CommitMessageContext:
    """What `commit_message.j2` can render.

    `files` lists every staged path, in the order git gives them. `not_shown` names the
    ones that have a diff that did not fit the budget. `previous` renders a paragraph
    asking for a different message only when it is not empty.
    """

    files: tuple[StagedFile, ...]
    not_shown: tuple[str, ...] = ()
    previous: str = ""


def staged(repo: Repo) -> list[NameStatus]:
    """The staged paths, renames detected. Refused with `GitRefused` when there are none,
    which the API answers with 409."""
    entries = parse_name_status(repo.git.diff("--cached", "-M", "--name-status", "-z"))
    if not entries:
        raise GitRefused(NOTHING_STAGED)
    return entries


def label_of(path: str, tree: Tree) -> str:
    """`path` named as the writer knows it, from the scan; the path itself when the scan
    has nothing there (a deleted file, or a path outside every story)."""
    parts = PurePosixPath(path).parts
    if not parts or parts[0] != STORIES:
        return path

    if len(parts) == 4 and parts[2] == CHAPTERS and path.endswith(CHAPTER_SUFFIX):
        chapter = tree.chapters.get(derive_id(SPACE, path))
        if chapter is None:
            return path
        story = tree.stories[chapter.story_id]
        ids = [file.id for file in story.files]
        position = ids.index(chapter.id) + 1
        return f'chapter {position} of {story.name} (titled "{chapter.name}")'

    if len(parts) == 2 and path.endswith(CHAPTER_SUFFIX):
        one_shot = tree.one_shots.get(derive_id(SPACE, path))
        return f'the one-shot "{one_shot.name}"' if one_shot is not None else path

    if len(parts) == 3 and parts[2] == MANIFEST:
        story = tree.stories.get(derive_id(SPACE, str(PurePosixPath(*parts[:2]))))
        return f"the manifest of {story.name}" if story is not None else path

    return path


def _text(repo: Repo, revision: str, path: str | None) -> str:
    """The text of `path` at `revision` (`HEAD`, or `""` for the index), `""` where it is
    not there."""
    if path is None:
        return ""
    try:
        return repo.git.show(f"{revision}:{path}")
    except GitCommandError:
        return ""


def _compared(path: str, text: str) -> str | None:
    """The part of `text` the diff compares: an `.ink` file's body, a `story.yaml` as it
    is, and nothing for any other path."""
    if path.endswith(CHAPTER_SUFFIX):
        return body_of(text)
    if PurePosixPath(path).name == MANIFEST:
        return text
    return None


def diff_of(repo: Repo, entry: NameStatus) -> str:
    """The unified diff of one staged entry's compared text, one line of context, without
    the `---`/`+++` header; `""` for a deletion or a path that is not compared."""
    if entry.new is None:
        return ""
    new = _compared(entry.new, _text(repo, "", entry.new))
    if new is None:
        return ""
    old = _compared(entry.new, _text(repo, "HEAD", entry.old)) or ""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), n=1, lineterm="")
    return "\n".join(line for line in lines if not line.startswith(("---", "+++")))


def assemble_commit_message(
    repo: Repo, scanner: Scanner, *, budget: int, previous: str = ""
) -> CommitMessageContext:
    """Every staged path labelled, and their diffs in order while they fit `budget`
    characters. A diff that does not fit is left out and named under "Not shown:", and
    the next one is tried."""
    tree = scanner.tree()
    files: list[StagedFile] = []
    not_shown: list[str] = []
    used = 0
    for entry in staged(repo):
        path = entry.new if entry.new is not None else entry.old
        assert path is not None
        diff = diff_of(repo, entry)
        if diff and used + len(diff) > budget:
            not_shown.append(path)
            diff = ""
        used += len(diff)
        moved = entry.code[:1] in ("R", "C")
        files.append(
            StagedFile(
                path=path,
                state=STATES.get(entry.code[:1], "modified"),
                label=label_of(path, tree),
                old_path=entry.old if moved else None,
                diff=diff,
            )
        )
    return CommitMessageContext(
        files=tuple(files), not_shown=tuple(not_shown), previous=previous.strip()
    )


def staged_tags(repo: Repo) -> tuple[str, ...]:
    """The tags of every staged `.ink` file, read from its header in the index, so a
    provider refusing one of them is never sent the others' diffs either."""
    tags: list[str] = []
    for entry in staged(repo):
        if entry.new is None or not entry.new.endswith(CHAPTER_SUFFIX):
            continue
        for tag in ink.tags_of(ink.parse(_text(repo, "", entry.new)).metadata):
            if tag not in tags:
                tags.append(tag)
    return tuple(tags)


def render_commit_message(context: CommitMessageContext) -> str:
    """Wraps `context` in the commit message instructions sent to the model."""
    return prompt_lib.JINJA.get_template(COMMIT_MESSAGE_TEMPLATE).render(ctx=context)


def clean_commit_message(raw: str) -> str:
    """`raw`, a small model's answer, turned into one line fit for the message field.

    Code fences and blank lines are dropped and the first remaining line is kept; then a
    surrounding quote, a "Commit message:" prefix and a trailing full stop are stripped,
    spaces collapsed, and the length capped. A line without one of the template's kinds
    is kept as answered: the writer edits it before committing. Answers "" for nothing
    usable, which the caller turns into a 502.
    """
    lines = [line.strip() for line in raw.splitlines()]
    first = next((line for line in lines if line and not line.startswith("```")), "")

    for opening, closing in (('"', '"'), ("'", "'"), ("`", "`"), ("“", "”"), ("‘", "’")):
        if len(first) >= 2 and first.startswith(opening) and first.endswith(closing):
            first = first[1:-1].strip()
            break

    prefix = "commit message:"
    if first.lower().startswith(prefix):
        first = first[len(prefix) :].strip()

    first = " ".join(first.rstrip(".").split())
    return first[:MAX_PROPOSAL_LENGTH].rstrip()


@router.post("/commit-message")
async def propose_commit_message(
    body: CommitMessageRequest = CommitMessageRequest(),
    *,
    request: Request,
    user: CurrentUser,
    scanner: ScannerDep,
    repo: RepoDep,
    service: ServiceDep,
    settings: SettingsDep,
) -> dict:
    """Asks the configured small model for a one-line message naming what is staged.

    Costs the same rate-limit token a generation does, as a title does. 409 without a
    small model and 409 with nothing staged; 403, before anything is sent, when the
    small model's provider refuses a tag of a staged chapter.
    """
    if not request.app.state.llm_limiter.consume(user.email):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")
    if settings.llm_small_model is None:
        raise HTTPException(status.HTTP_409_CONFLICT, NO_SMALL_MODEL)

    prompt = render_commit_message(
        assemble_commit_message(
            repo,
            scanner,
            budget=service.defaults.prompt_budget,
            previous=body.previous or "",
        )
    )
    try:
        raw = await service.complete(
            settings.llm_small_model,
            prompt,
            tags=staged_tags(repo),
            options=service.defaults.overlay(think=False),
        )
    except UnknownModel as error:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except RefusedProvider as error:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(error)) from error
    except LLMError as error:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)) from error

    message = clean_commit_message(raw)
    if not message:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, NOTHING_USABLE)
    return {"message": message}
