# -*- coding: utf-8 -*-
"""The editor's pair of routes: prose and its provenance, together.

    GET  /api/stories/file/{id}/document     {"body", "metadata", "reconciled"}
    PUT  /api/stories/file/{id}/document     {"body", "metadata"}
    GET  /api/notes/file/{id}/document       the same, on the other root
    PUT  /api/notes/file/{id}/document

Both parts move in one request each way, because a section derived from the body may not
be written without it (§7.1). `PUT /file/{id}/contents` is gone for that reason: it
wrote prose and left the provenance hashes describing text that was no longer there.
Reading prose alone is still fine — `GET /file/{id}/contents` is untouched, and is what
the reading view, the word count and the generation path use.

This is its own module rather than part of `files.py` because it is the only thing
needing both a scanner and the git repository, and `repository.py` already imports
`files.py` — putting it there would close the cycle.

**A `GET` recovers but never writes.** A chapter whose prose was edited outside the
editor has provenance keyed to text that is gone, and `recover` replays it forward from
history (§7.5) so the writer never sees the drift. Recovering on read must not dirty the
story repository, though, so the answer is reconciled and the file is left stale on disk
until the next ordinary save persists it. The notes root has no history at all — it is
not a repository — so a paragraph it cannot account for resets.

**A `PUT` also schedules a background context-summary call (api#18)**, when
`INKSPIRE_LLM_SMALL_MODEL` is configured: `summaries.update` runs after the response has
already been sent, and may write a fresh `ink:context_summary` section a few seconds
later. This is the one section ever written outside the body's own atomic write
(`summaries.py` and `docs/ink-format.md`'s "Two rules" have why that is safe here and
nowhere else), which is why it is scheduled from this module rather than left to the
generate route that reads it back.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from git import Repo
from pydantic import BaseModel, Field

from . import provenance, repository, summaries
from .deps import CurrentUser, SettingsDep
from .files import NotesDep, ScannerDep
from .fs import MAX_FILE_BYTES
from .llm import ServiceDep
from .notes import NotesScanner
from .storage import Scanner

stories_router = APIRouter(prefix="/stories", tags=["stories"])
notes_router = APIRouter(prefix="/notes", tags=["notes"])

TOO_LONG = f"A file must be at most {MAX_FILE_BYTES} bytes."

#: File ids with a context-summary call already running, so a second save landing
#: before the first call answers is skipped rather than queued (api#18) -- the
#: summary is then one save behind until the next one catches up. A plain set needs
#: no lock of its own: `BackgroundTasks` runs these on the event loop, never two at
#: once in the same process.
_SUMMARIZING: set[str] = set()


class DocumentBody(BaseModel):
    """What a save sends: the prose, and the provenance it goes with.

    `metadata` of `None` removes the section — a file the editor has nothing to record
    about. `{}` is a different thing: a file whose prose is all hand-written, which is
    worth recording, because it says the editor has been here.
    """

    body: str = Field(default="")
    metadata: dict[str, list[tuple[int, int, str]]] | None = None


def _reconciled(stored: provenance.Metadata, answered: provenance.Metadata, revision: str | None):
    """What a load had to put right, or `None` where it had nothing to.

    Named per paragraph so a later interface can say so; nothing displays it today. The
    keys are compared rather than the runs, because a paragraph whose hash still matches
    keeps its runs untouched by definition — a key appearing or disappearing is exactly
    what a hand edit does.
    """
    if set(stored) == set(answered):
        return None
    return {
        "revision": revision,
        "recovered": [key for key in answered if key not in stored and answered[key]],
        "reset": [key for key in answered if key not in stored and not answered[key]],
        "dropped": [key for key in stored if key not in answered],
    }


def _read(
    scanner: Scanner | NotesScanner, file_id: str, repo: Repo | None
) -> dict:
    """One file's prose and provenance, reconciled against what the prose says now."""
    document = scanner.read_document(file_id)
    stored = (
        provenance.parse_section(document.provenance)
        if document.provenance is not None
        else None
    )
    if stored is None:
        return {"body": document.body, "metadata": None, "reconciled": None}

    recovery = repository.reconciled(repo, PurePosixPath(document.relpath), document.body, stored)
    return {
        "body": document.body,
        "metadata": {
            key: [list(run) for run in runs] for key, runs in recovery.metadata.items()
        },
        "reconciled": _reconciled(stored, recovery.metadata, recovery.revision),
    }


async def _write(
    scanner: Scanner | NotesScanner,
    file_id: str,
    document: DocumentBody,
    *,
    background_tasks: BackgroundTasks,
    request: Request,
    user: CurrentUser,
    service: ServiceDep,
    settings: SettingsDep,
) -> dict:
    """One file's prose and provenance, written together; schedules a background
    context-summary call (api#18) afterwards."""
    if len(document.body.encode("utf-8")) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LONG)

    section = (
        None if document.metadata is None else provenance.render_section(document.metadata)
    )
    # Off the event loop: the write would otherwise hold up every other request being
    # served alongside it.
    await run_in_threadpool(scanner.write_document, file_id, document.body, section)
    _schedule_summary(
        scanner,
        file_id,
        background_tasks=background_tasks,
        request=request,
        user=user,
        service=service,
        settings=settings,
    )
    return {"ok": True}


def _schedule_summary(
    scanner: Scanner | NotesScanner,
    file_id: str,
    *,
    background_tasks: BackgroundTasks,
    request: Request,
    user: CurrentUser,
    service: ServiceDep,
    settings: SettingsDep,
) -> None:
    """Schedules `summaries.update` for `file_id` after this response is sent, unless
    one of three things says not to (api#18): no small model is configured, one is
    already running for this file, or this account's own summary rate budget
    (`app.state.summary_limiter`, separate from generation's own) is spent. Each of
    these skips silently -- there is no response left to answer a refusal into, and
    the next save's own call catches up regardless.
    """
    model = settings.llm_small_model
    if model is None or file_id in _SUMMARIZING:
        return
    if not request.app.state.summary_limiter.consume(user.email):
        return

    budget = service.defaults.prompt_budget

    async def _run() -> None:
        _SUMMARIZING.add(file_id)
        try:
            await summaries.update(scanner, file_id, service=service, model=model, budget=budget)
        finally:
            _SUMMARIZING.discard(file_id)

    background_tasks.add_task(_run)


@stories_router.get("/file/{file_id}/document")
def read_story_document(file_id: str, scanner: ScannerDep, repo: repository.OptionalRepoDep) -> dict:
    """A chapter's or a one-shot's prose and provenance, recovered where it is stale."""
    return _read(scanner, file_id, repo)


@stories_router.put("/file/{file_id}/document")
async def write_story_document(
    file_id: str,
    body: DocumentBody,
    scanner: ScannerDep,
    background_tasks: BackgroundTasks,
    *,
    request: Request,
    user: CurrentUser,
    service: ServiceDep,
    settings: SettingsDep,
) -> dict:
    """Replaces a chapter's or a one-shot's prose and provenance in one write."""
    return await _write(
        scanner,
        file_id,
        body,
        background_tasks=background_tasks,
        request=request,
        user=user,
        service=service,
        settings=settings,
    )


@notes_router.get("/file/{file_id}/document")
def read_note_document(file_id: str, notes: NotesDep) -> dict:
    """A note's prose and provenance. No repository, so a stale paragraph resets."""
    return _read(notes, file_id, None)


@notes_router.put("/file/{file_id}/document")
async def write_note_document(
    file_id: str,
    body: DocumentBody,
    notes: NotesDep,
    background_tasks: BackgroundTasks,
    *,
    request: Request,
    user: CurrentUser,
    service: ServiceDep,
    settings: SettingsDep,
) -> dict:
    """Replaces a note's prose and provenance in one write."""
    return await _write(
        notes,
        file_id,
        body,
        background_tasks=background_tasks,
        request=request,
        user=user,
        service=service,
        settings=settings,
    )
