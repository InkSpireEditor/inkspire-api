# -*- coding: utf-8 -*-
"""The story repository as git, and the routes that drive it.

    GET  /api/git/status                    the branch, what has changed, ahead/behind
    POST /api/git/commit    {"message"}      stage and commit only what the API writes
    POST /api/git/push                       push the branch to its upstream
    POST /api/git/pull                       fast-forward onto the upstream

    GET  /api/stories/file/{id}/history      the chapter's commits, newest first
    GET  /api/stories/file/{id}/at/{rev}     that chapter's prose at one commit

`INKSPIRE_DATA_ROOT` is a git working tree, and this is the only module that knows
that: `storage.py` walks it as a filesystem and never runs `git`. Saving a chapter
never commits — only the button behind `/commit` does, and only onto the two kinds of
path the API itself writes: a story's `story.yaml` and its `chapters/*.ink`. A
hand-edited lorebook, a `timeline.yaml`, or anything staged in a shell is left exactly
as it was.

`/status` never fetches, so `ahead`/`behind` are only as fresh as the last pull. A
pull that cannot fast-forward is refused rather than merged, and a push the remote
rejects is refused rather than retried — both leave the working tree untouched and
name what to do about it in a shell.

The client never sends a path. `/history` and `/at/{rev}` resolve a chapter's id to
its current path and walk its log with `--follow`, so a rev from before a rename still
answers with that chapter's prose at the time, and a rev outside its history is a 404
rather than a read of whatever else happens to be at that path.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Annotated, NamedTuple

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError
from pydantic import BaseModel, StringConstraints

from . import ink, provenance
from .deps import SettingsDep
from .files import ScannerDep
from .fs import MAX_COMMIT_MESSAGE_LENGTH, NotFound, StorageError, derive_id
from .settings import Settings
from .storage import CHAPTER_SUFFIX, CHAPTERS, MANIFEST, SPACE, STORIES, Chapter, Scanner

router = APIRouter(prefix="/git", tags=["git"])
history_router = APIRouter(prefix="/stories/file", tags=["history"])

#: A commit's sha, as `/history` hands it out and `/at/{rev}` accepts it back. Bounded
#: so a rev cannot be read as a git option and a branch name cannot smuggle a `/` into
#: a path segment.
Rev = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{7,40}$")]

Message = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_COMMIT_MESSAGE_LENGTH),
]


class NotARepository(StorageError):
    """The configured story repository is not the root of its own git working tree."""


class GitIdentityMissing(StorageError):
    """Nothing says who a commit is from: not the settings, not the repository's own
    git config, not the global one either."""


class GitRefused(StorageError):
    """The repository refused the operation: nothing to commit, no branch, no
    upstream, or a push or pull that would need a merge."""


class RemoteFailed(StorageError):
    """The remote could not be reached, or rejected the request for a reason other
    than the history having moved."""


class GitTimeout(StorageError):
    """A git command did not finish within the configured timeout."""


class CommitBody(BaseModel):
    """What to commit. Only the message: the paths are decided by what changed."""

    message: Message


# --- opening the repository --------------------------------------------------


@contextmanager
def open_repository(root: Path) -> Iterator[Repo]:
    """The story repository, refused if `root` is not the root of its own git tree.

    Opened with no parent search, so a root that is not itself a git repository never
    falls through to one above it — this project's own source tree included, were
    `INKSPIRE_DATA_ROOT` ever left at its relative default. The check on
    `working_tree_dir` is the same guarantee stated explicitly, in case a future
    GitPython version changes what a bare `Repo(root)` resolves to.
    """
    try:
        repo = Repo(root, search_parent_directories=False)
    except (InvalidGitRepositoryError, NoSuchPathError) as error:
        raise NotARepository(
            f"{root} is not a git repository. Clone the story repository there."
        ) from error
    if repo.working_tree_dir is None or Path(repo.working_tree_dir).resolve() != root.resolve():
        repo.close()
        raise NotARepository(f"{root} is not the root of its own git repository.")
    try:
        yield repo
    finally:
        repo.close()


def get_repository(settings: SettingsDep) -> Iterator[Repo]:
    """The story repository, opened fresh for the request. Cheap next to what this
    router does with it, so unlike `Scanner` it is not held between requests."""
    with open_repository(settings.data_root) as repo:
        yield repo


RepoDep = Annotated[Repo, Depends(get_repository)]


def get_optional_repository(settings: SettingsDep) -> Iterator[Repo | None]:
    """The story repository where there is one, and `None` where there is not.

    For a route whose work git only improves. Opening a chapter must not fail because
    the data root was never cloned — provenance recovery is the one thing lost, and a
    paragraph that cannot be recovered resets (§7.5), which is a working answer. The
    `/git/*` routes keep `RepoDep` and still refuse outright, since without a
    repository they have nothing to say at all.
    """
    try:
        with open_repository(settings.data_root) as repo:
            yield repo
    except NotARepository:
        yield None


OptionalRepoDep = Annotated[Repo | None, Depends(get_optional_repository)]


def _locked(request: Request) -> Iterator[None]:
    """Refuses a second git-changing request while one is already running.

    Commit, push and pull each take this for the whole request, non-blocking: a
    second request fails immediately rather than queuing behind the first one's
    timeout on a thread-pool worker that could otherwise be serving something else.
    """
    lock: threading.Lock = request.app.state.git_lock
    if not lock.acquire(blocking=False):
        raise GitRefused("Another git operation is already running. Try again shortly.")
    try:
        yield
    finally:
        lock.release()


LockDep = Annotated[None, Depends(_locked)]


# --- what the button is allowed to touch -------------------------------------


def _committable(path: str) -> bool:
    """Whether `/commit` will stage this path: a story's own manifest, a chapter, or a
    one-shot loose at the stories root.

    A literal check on the path's parts rather than a git pathspec, because a glob
    pathspec matches across `/` and would not actually express "one level deep".
    """
    parts = PurePosixPath(path).parts
    if len(parts) == 2 and parts[0] == STORIES and parts[1].endswith(CHAPTER_SUFFIX):
        return True
    if len(parts) == 3 and parts[0] == STORIES and parts[2] == MANIFEST:
        return True
    return (
        len(parts) == 4
        and parts[0] == STORIES
        and parts[2] == CHAPTERS
        and parts[3].endswith(CHAPTER_SUFFIX)
    )


def _dir_id(path: str) -> str | None:
    """The story id a path sits under, or `None` for a path outside every story."""
    parts = PurePosixPath(path).parts
    if len(parts) >= 2 and parts[0] == STORIES:
        return derive_id(SPACE, str(PurePosixPath(STORIES) / parts[1]))
    return None


# --- status -------------------------------------------------------------------


def _parse_status(raw: str) -> list[tuple[str, str]]:
    """`(code, path)` pairs from `git status --porcelain -z`.

    `-z` null-terminates every field rather than escaping the path, and a rename or a
    copy carries its old path as a second field before the next entry — without
    consuming it, that old path would be read back as the next entry's own code.
    """
    tokens = raw.split("\0")
    pairs: list[tuple[str, str]] = []
    i = 0
    while i < len(tokens) and tokens[i]:
        code, path = tokens[i][:2], tokens[i][3:]
        i += 1
        if code[0] in ("R", "C"):
            i += 1
        pairs.append((code, path))
    return pairs


def _state(code: str) -> str:
    """One of `added`, `modified`, `deleted`, `renamed`, `untracked`, `conflicted`,
    from a porcelain status code's two characters."""
    left, right = code[0], code[1]
    if left == "?" and right == "?":
        return "untracked"
    if "U" in (left, right) or (left, right) in (("A", "A"), ("D", "D")):
        return "conflicted"
    if "R" in (left, right):
        return "renamed"
    if "A" in (left, right):
        return "added"
    if "D" in (left, right):
        return "deleted"
    return "modified"


def status(repo: Repo) -> dict:
    """The branch, what has changed, and how far it is from its upstream.

    Never fetches: `ahead`/`behind` are counted against the upstream ref as it stands
    on disk, which is only as fresh as the last pull.
    """
    if repo.head.is_detached:
        raise GitRefused("HEAD does not point at a branch. Check one out first.")
    branch = repo.active_branch
    tracking = branch.tracking_branch()

    ahead = behind = None
    if tracking is not None:
        ahead = sum(1 for _ in repo.iter_commits(f"{tracking.name}..{branch.name}"))
        behind = sum(1 for _ in repo.iter_commits(f"{branch.name}..{tracking.name}"))

    changes = [
        {
            "path": path,
            "state": _state(code),
            "staged": code[0] not in (" ", "?"),
            "dir": _dir_id(path),
            "committable": _committable(path),
        }
        for code, path in _parse_status(
            repo.git.status("--porcelain", "-z", "--untracked-files=all")
        )
    ]

    return {
        "branch": branch.name,
        "upstream": tracking.name if tracking is not None else None,
        "ahead": ahead,
        "behind": behind,
        "fetched": False,
        "clean": not changes,
        "changes": changes,
    }


# --- running a bounded git command --------------------------------------------


def _run(call: Callable[..., str], *args: str, timeout: float) -> str:
    """Runs one git command that can genuinely hang — one invoking a hook, or one
    talking to a remote — killing it if it outruns `timeout`.

    A local read (`status`, `log`, `show`) is not bounded this way: nothing short of
    a pathological repository makes one hang, and `kill_after_timeout` is not free —
    it is also not available on Windows, which is not a deployment target here.
    """
    try:
        return call(*args, kill_after_timeout=timeout)
    except GitCommandError as error:
        if "Timeout: the command" in str(error):
            raise GitTimeout(f"Git did not finish within {timeout:g} seconds.") from error
        raise


def _explanation(error: GitCommandError) -> str:
    """Git's own stderr, without the argv dump GitPython wraps around it."""
    text = str(error)
    marker = "stderr: '"
    if marker in text:
        return text.split(marker, 1)[1].rstrip("'\n ")
    return text


def _identity(repo: Repo, settings: Settings) -> dict[str, str]:
    """Author and committer environment for one commit.

    `INKSPIRE_GIT_AUTHOR_NAME`/`_EMAIL` win; left unset, whatever `git config` already
    resolves for this repository — its own config, then global, then system — is used
    instead, exactly as running `git commit` by hand here would.
    """
    name = settings.git_author_name
    email = settings.git_author_email
    if not name or not email:
        reader = repo.config_reader()
        name = name or reader.get_value("user", "name", default=None)
        email = email or reader.get_value("user", "email", default=None)
    if not name or not email:
        raise GitIdentityMissing(
            "No commit identity configured. Set INKSPIRE_GIT_AUTHOR_NAME and "
            "INKSPIRE_GIT_AUTHOR_EMAIL, or this repository's own user.name and "
            "user.email."
        )
    return {
        "GIT_AUTHOR_NAME": str(name),
        "GIT_AUTHOR_EMAIL": str(email),
        "GIT_COMMITTER_NAME": str(name),
        "GIT_COMMITTER_EMAIL": str(email),
    }


# --- commit, push, pull --------------------------------------------------------


def commit(repo: Repo, settings: Settings, message: str) -> dict:
    """Stages and commits only the committable paths, and answers the new status.

    `--only` commits exactly the named paths, so anything staged by hand alongside
    them is left staged rather than swept into this commit; `git add -A --` restricted
    to the same paths is what stages them, additions and deletions alike.
    """
    paths = [change["path"] for change in status(repo)["changes"] if change["committable"]]
    if not paths:
        raise GitRefused("Nothing committable has changed.")

    identity = _identity(repo, settings)
    timeout = settings.git_timeout
    try:
        with repo.git.custom_environment(**identity):
            _run(repo.git.add, "-A", "--", *paths, timeout=timeout)
            _run(repo.git.commit, "--only", "-m", message, "--", *paths, timeout=timeout)
    except GitCommandError as error:
        raise GitRefused(_explanation(error)) from error

    result = status(repo)
    result["sha"] = repo.head.commit.hexsha
    result["message"] = message
    result["committed"] = paths
    return result


def push(repo: Repo, settings: Settings) -> dict:
    """Pushes the current branch to its upstream, and answers the new status."""
    before = status(repo)
    if before["upstream"] is None:
        raise GitRefused("This branch has no upstream to push to.")

    try:
        _run(repo.git.push, timeout=settings.git_timeout)
    except GitCommandError as error:
        text = _explanation(error)
        if "rejected" in text.lower() or "non-fast-forward" in text.lower():
            raise GitRefused(
                f"The remote has commits this branch does not "
                f"({before['behind']} behind). Pull first."
            ) from error
        raise RemoteFailed(text) from error

    return status(repo)


def pull(repo: Repo, scanner: Scanner, settings: Settings) -> dict:
    """Fast-forwards onto the upstream, refusing anything that would need a merge.

    Invalidates the scan on success: a pull is not a change made through `Scanner`,
    and its own stamp is only as fresh as the last time anything asked for the tree.
    """
    before = status(repo)
    if before["upstream"] is None:
        raise GitRefused("This branch has no upstream to pull from.")

    try:
        _run(repo.git.pull, "--ff-only", timeout=settings.git_timeout)
    except GitCommandError as error:
        text = _explanation(error)
        if "fast-forward" in text.lower() or "diverged" in text.lower():
            raise GitRefused(
                f"The branch has diverged from {before['upstream']} "
                f"({before['ahead']} ahead, {before['behind']} behind). "
                "Resolve it in a shell."
            ) from error
        raise RemoteFailed(text) from error

    scanner.invalidate()
    return status(repo)


# --- history --------------------------------------------------------------------


def _log_records(repo: Repo, relpath: PurePosixPath, *, limit: int | None) -> list[dict]:
    """Every commit touching `relpath`, newest first, following it across a rename.

    `iter_commits` runs `git rev-list`, which has no `--follow` — only `git log` does
    — so this shells out directly. `--name-only` under `--follow` reports the path the
    file had at each commit, which is what `content_at` needs to `git show` the right
    blob for a rev from before a rename.
    """
    # `%x01`/`%x00` are git's own escape for a literal byte in the *output*; writing
    # the byte itself into the argument would put a NUL inside an argv string, which
    # a real exec() cannot carry at all.
    args = ["--follow", "--format=%x01%H%x00%h%x00%an%x00%aI%x00%s", "--name-only"]
    if limit is not None:
        args.append(f"--max-count={limit}")
    raw = repo.git.log(*args, "--", str(relpath))

    records = []
    for block in raw.split("\x01"):
        if not block:
            continue
        header, _, name = block.partition("\n\n")
        sha, short, author, date, message = header.split("\x00")
        records.append(
            {
                "sha": sha,
                "short": short,
                "author": author,
                "date": date,
                "message": message,
                "path": name.strip(),
            }
        )
    return records


def history(repo: Repo, chapter: Chapter, *, limit: int) -> list[dict]:
    """The chapter's commits, newest first, without the path each had — the client
    never sees a path, only ids and revs."""
    return [
        {key: record[key] for key in ("sha", "short", "author", "date", "message")}
        for record in _log_records(repo, chapter.relpath, limit=limit)
    ]


class Recovery(NamedTuple):
    """What a reconciliation answered.

    `sources` maps a paragraph's new hash to the stale key its runs came from. Only
    `ink maintenance` reads it, to say in its report which key it recovered — but it has
    to come from here, because the pairing is decided by similarity (§7.5) and a caller
    comparing key sets afterwards could only guess at it.
    """

    #: The metadata the file should hold, keyed by the paragraphs it has now.
    metadata: provenance.Metadata
    #: The newest revision that explained anything, or `None` if none did.
    revision: str | None
    #: New hash to the stale key it was recovered from, for whatever was. Required
    #: rather than defaulted: a mutable default on a NamedTuple is one object shared by
    #: every instance that takes it.
    sources: dict[str, str]


#: How many revisions back a provenance recovery looks before giving up.
#:
#: The walk costs one blob read per revision, so without a ceiling a chapter with
#: thousands of commits and one stale paragraph from the first of them would make
#: thousands of `git show` calls inside a single `GET`. A wall-clock timeout is the wrong
#: bound here and `_run` says why: a local read does not hang, so what has to be limited
#: is the work, not the waiting. Fifty revisions is far past the few a hand edit sits
#: behind; past that, the paragraph resets.
MAX_RECOVERY_REVISIONS = 50

#: How alike a stored paragraph and a current one must be to be called the same
#: paragraph, edited. Below this the pairing would be a guess, and a wrong pairing
#: writes a confident account of something that never happened — so the paragraph resets
#: instead. `reconcile` itself is correct for any pair; this guards only the pairing.
MIN_PAIRING_SIMILARITY = 0.5


def _paragraphs_at(repo: Repo, record: dict) -> list[str]:
    """The paragraphs of one revision of a file, or none if that blob cannot be read."""
    try:
        text = repo.git.show(f"{record['sha']}:{record['path']}")
    except GitCommandError:
        return []
    paragraphs, _ = provenance.split_paragraphs(ink.parse(text).body)
    return paragraphs


def _stale_texts(
    repo: Repo, relpath: PurePosixPath, wanted: set[str], limit: int | None
) -> tuple[dict[str, str], str | None]:
    """The text each hash in `wanted` had, found by walking the file's history.

    Newest first, stopping as soon as every wanted hash is accounted for, so the cost is
    the revisions since the edit rather than the length of the history. Answers what was
    found and the newest revision that contributed — `None` where nothing did.

    `limit` of `None` walks the whole history, which only `ink maintenance` asks for: it
    is run by hand, after a miss, and is worth however long it takes. A request takes the
    ceiling instead.

    Takes no lock. The commit/push/pull lock serialises writes, and opening a chapter
    must not fail because a push is running.
    """
    try:
        records = _log_records(repo, relpath, limit=None)
    except GitCommandError:
        return {}, None

    found: dict[str, str] = {}
    revision: str | None = None
    for record in (records if limit is None else records[:limit]):
        for paragraph in _paragraphs_at(repo, record):
            digest = provenance.paragraph_hash(paragraph)
            if digest in wanted and digest not in found:
                found[digest] = paragraph
                if revision is None:
                    revision = record["short"]
        if len(found) == len(wanted):
            break
    return found, revision


def _best_match(paragraph: str, found: dict[str, str]) -> str | None:
    """Which of the stale paragraphs `paragraph` is the edited version of, if any.

    Greedy in the body's own order, and each stale text is used once — two paragraphs
    cannot both be what one stored paragraph became.
    """
    best: str | None = None
    score = MIN_PAIRING_SIMILARITY
    for key, text in found.items():
        ratio = provenance.similarity(text, paragraph)
        if ratio >= score:
            best, score = key, ratio
    return best


def recover(
    repo: Repo,
    relpath: PurePosixPath,
    body: str,
    metadata: provenance.Metadata,
    *,
    limit: int | None = MAX_RECOVERY_REVISIONS,
) -> Recovery:
    """`metadata` rekeyed to `body`'s paragraphs, recovering what history can explain.

    A paragraph whose hash is stored keeps its runs untouched. A paragraph whose hash is
    not is stale — its text was edited outside the editor — so the stored key's own text
    is looked for in the file's history, diffed forward to what the paragraph says now,
    and its runs replayed over the result (§7.5). What history cannot explain resets to
    an empty list, which renders as plain prose. Nothing is invented.

    Answers the new metadata and the revision it was recovered from, or `None` where
    nothing was recovered — either because nothing was stale or because the walk found
    no match.

    **Writes nothing.** A read must not dirty the story repository, so the file stays
    stale on disk until the next ordinary save; a bare `ink maintenance` is the only
    thing that persists a recovery on its own.
    """
    paragraphs, _ = provenance.split_paragraphs(body)
    hashes = [provenance.paragraph_hash(paragraph) for paragraph in paragraphs]
    stale = {digest for digest in metadata if digest not in set(hashes)}

    nothing_to_do = not stale or all(digest in metadata for digest in hashes)
    if nothing_to_do:
        return Recovery(provenance.without_recovery(body, metadata), None, {})

    found, revision = _stale_texts(repo, relpath, stale, limit)
    if not found:
        return Recovery(provenance.without_recovery(body, metadata), None, {})

    return _replayed(zip(hashes, paragraphs), metadata, found, revision)


def _replayed(
    paragraphs: Iterator[tuple[str, str]],
    metadata: provenance.Metadata,
    found: dict[str, str],
    revision: str | None,
) -> Recovery:
    """Each paragraph's runs: its own where its hash is stored, replayed where it is not.

    `found` is consumed as it goes, so one stale text explains one paragraph and no
    more — two paragraphs cannot both be what one stored paragraph became.
    """
    runs: dict[str, list[provenance.Run]] = {}
    sources: dict[str, str] = {}
    for digest, paragraph in paragraphs:
        if digest in runs:
            # Two identical paragraphs share one entry (§7.3). Without this the second
            # would find the stale text already claimed and overwrite the recovery with
            # an empty list.
            continue
        if digest in metadata:
            runs[digest] = list(metadata[digest])
            continue
        key = _best_match(paragraph, found)
        if key is None:
            runs[digest] = []
            continue
        runs[digest] = provenance.reconcile(found.pop(key), metadata[key], paragraph)
        sources[digest] = key
    return Recovery(runs, revision, sources)


def reconciled(
    repo: Repo | None,
    relpath: PurePosixPath,
    body: str,
    metadata: provenance.Metadata,
    *,
    limit: int | None = MAX_RECOVERY_REVISIONS,
) -> Recovery:
    """`metadata` rekeyed to `body`, recovering from history where there is any.

    The one place that decides what no repository means, so the `document` route and
    `ink maintenance` cannot come to different answers about it. `None` for `repo` is the
    notes root, whose files have no history, and a story root nobody has cloned: in
    either case a paragraph that is no longer accounted for resets (§7.5).
    """
    if repo is None:
        return Recovery(provenance.without_recovery(body, metadata), None, {})
    return recover(repo, relpath, body, metadata, limit=limit)


def content_at(repo: Repo, chapter: Chapter, rev: str) -> str:
    """The chapter's prose at `rev`, header stripped, as `/contents` is.

    Resolved through the chapter's own `--follow`ed history rather than trusting
    `rev`'s path to still be the chapter's current one: a rev from before a rename
    named a different path, and `rev` naming a commit outside this chapter's history
    at all is a 404 rather than a read of whatever else that commit touched.
    """
    match = next(
        (
            record
            for record in _log_records(repo, chapter.relpath, limit=None)
            if record["sha"].startswith(rev)
        ),
        None,
    )
    if match is None:
        raise NotFound(f'"{rev}" is not a commit in this chapter\'s history.')
    text = repo.git.show(f"{match['sha']}:{match['path']}")
    return ink.parse(text).body


# --- the story repository's history --------------------------------------------


#: The most commits the activity reader looks at, newest first, whatever the window
#: asks for. A history with more commits than this in the window is cut at its oldest
#: end and the days that lose are the oldest ones; `docs/routes.md` says so.
MAX_ACTIVITY_COMMITS = 5000


def history_records(
    repo: Repo, since: date
) -> Iterator[tuple[date, list[tuple[str, str]]]]:
    """Each non-merge commit's `(author date, (old text, new text) per file)`, newest first.

    The files are the `.ink` ones under `stories/` -- chapters and one-shots; nothing
    else. `old` is the file as the commit's first parent has it, `new` as the commit has
    it, either `""` when there is no such blob (a new file, a deleted one, or a commit
    with no parent). The texts are the whole files, sections included: a caller that
    wants the prose parses them. `since` is the window's first day, by author date.

    `iter_commits` runs `git rev-list`, whose `--since` filters on the **committer**
    date. A commit's committer date is never earlier than its author date in practice,
    so every commit authored inside the window is read here, and those authored before
    `since` are dropped by author date. The root commit is included: `iter_commits("HEAD")`
    walks it like any other. A repository with no commits yields nothing.

    A generator, so a caller can count each commit's texts and let them go instead of
    holding every commit's files at once.
    """
    if not repo.head.is_valid():
        return
    for revision in repo.iter_commits(
        "HEAD", since=since.isoformat(), no_merges=True, max_count=MAX_ACTIVITY_COMMITS
    ):
        day = revision.authored_datetime.date()
        if day < since:
            continue
        yield day, _changes_of(repo, revision)


class NameStatus(NamedTuple):
    """One line of `git diff-tree --name-status`: what happened, and to which paths.

    `old` is `None` for an added file and `new` is `None` for a deleted one. A rename
    carries both. A modification or a type change carries the one path twice.
    """

    code: str
    old: str | None
    new: str | None


def parse_name_status(raw: str) -> list[NameStatus]:
    """The entries of `git diff-tree --name-status -z` output, which has no commit header.

    With `-z` the output is NUL-separated: a status, then one path, or two for a rename
    or a copy (`R100`, `C75`), then the next status.
    """
    tokens = raw.split("\0")
    if tokens and tokens[-1] == "":
        tokens.pop()

    entries: list[NameStatus] = []
    i = 0
    while i < len(tokens):
        code = tokens[i]
        i += 1
        if code[:1] in ("R", "C"):
            entries.append(NameStatus(code, tokens[i], tokens[i + 1]))
            i += 2
        elif code[:1] == "A":
            entries.append(NameStatus(code, None, tokens[i]))
            i += 1
        elif code[:1] == "D":
            entries.append(NameStatus(code, tokens[i], None))
            i += 1
        else:
            entries.append(NameStatus(code, tokens[i], tokens[i]))
            i += 1
    return entries


def _changes_of(repo: Repo, revision) -> list[tuple[str, str]]:
    """`(old text, new text)` per `.ink` file under `stories/` that `revision` changed.

    `diff-tree --root` treats the root commit as a change from nothing, which is what a
    new file needs, and `--no-commit-id` leaves out the line naming the commit, so the
    output is only entries. `-M` turns a rename into one `R` entry carrying both paths,
    so the old text is read from the old path and a rename that edits nothing adds
    nothing. The old text is read from the commit's first parent's tree and the new text
    from the commit's own tree; either side is `""` where it has no such blob.
    """
    raw = repo.git.diff_tree(
        "--root", "--no-commit-id", "-r", "-M", "--name-status", "-z", revision.hexsha
    )
    parent_tree = revision.parents[0].tree if revision.parents else None

    changes: list[tuple[str, str]] = []
    for entry in parse_name_status(raw):
        if not (_is_story_ink(entry.old) or _is_story_ink(entry.new)):
            continue
        changes.append((_blob_text(parent_tree, entry.old), _blob_text(revision.tree, entry.new)))
    return changes


def _is_story_ink(path: str | None) -> bool:
    """Whether `path` is an `.ink` file under `stories/` -- a chapter or a one-shot."""
    if path is None:
        return False
    parts = PurePosixPath(path).parts
    return (
        len(parts) >= 2
        and parts[0] == STORIES
        and parts[-1].endswith(CHAPTER_SUFFIX)
    )


def _blob_text(tree, path: str | None) -> str:
    """A file's text as `tree` has it, or `""` when the tree has no such blob.

    Read through GitPython's object reader, which keeps one `cat-file --batch` process
    open, rather than one `git show` per blob. A blob that is not valid UTF-8 counts as
    `""`, since prose that cannot be read adds no words.
    """
    if path is None or tree is None:
        return ""
    try:
        blob = tree / path
        return blob.data_stream.read().decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        return ""


# --- routes -----------------------------------------------------------------


@router.get("/status")
def git_status(repo: RepoDep) -> dict:
    """The branch, what has changed, and how far it is from its upstream.

    A hand-edited lorebook or `timeline.yaml` shows here like anything else, with
    `committable: false` — dirty, and not what the button below is about to commit.
    """
    return status(repo)


@router.post("/commit")
def git_commit(body: CommitBody, settings: SettingsDep, repo: RepoDep, _lock: LockDep) -> dict:
    """Commits only what the API itself wrote: a story's manifest and its chapters."""
    return commit(repo, settings, body.message)


@router.post("/push")
def git_push(settings: SettingsDep, repo: RepoDep, _lock: LockDep) -> dict:
    """Pushes the branch to its upstream."""
    return push(repo, settings)


@router.post("/pull")
def git_pull(scanner: ScannerDep, settings: SettingsDep, repo: RepoDep, _lock: LockDep) -> dict:
    """Fast-forwards onto the upstream. A pull that would need a merge is refused."""
    return pull(repo, scanner, settings)


@history_router.get("/{file_id}/history")
def file_history(
    file_id: str,
    scanner: ScannerDep,
    repo: RepoDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict]:
    """The chapter's commits, newest first, following it across a rename."""
    return history(repo, scanner.chapter(file_id), limit=limit)


@history_router.get("/{file_id}/at/{rev}", response_class=PlainTextResponse)
def file_at_rev(file_id: str, rev: Rev, scanner: ScannerDep, repo: RepoDep) -> PlainTextResponse:
    """That chapter's prose at one commit, as `text/plain`, header stripped."""
    return PlainTextResponse(content_at(repo, scanner.chapter(file_id), rev))
