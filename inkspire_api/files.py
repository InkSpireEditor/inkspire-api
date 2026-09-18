# -*- coding: utf-8 -*-
"""File and directory routes, for the two roots the API serves.

The stories, where a directory is a story and its files are that story's chapters:

    GET    /api/stories/tree                  every story
    GET    /api/stories/dir/{id}              one story, and the chapters in it
    POST   /api/stories/dir                   create a story
    PUT    /api/stories/dir/{id}              retitle a story, or rewrite its synopsis
    DELETE /api/stories/dir/{id}              delete a story and its chapters
    POST   /api/stories/file                  create a chapter in a story
    GET    /api/stories/file/{id}             one chapter's name, status and summary
    PUT    /api/stories/file/{id}             rename a chapter, or move it to another story
    DELETE /api/stories/file/{id}             delete a chapter
    GET    /api/stories/file/{id}/contents    the chapter's prose, as text/plain
    PUT    /api/stories/file/{id}/contents    replace that prose with the request body

And everything that is not a novel, under `/api/notes/...`, the same routes with two
differences: a file may sit at the root, so `POST` accepts `dir: null` and the tree's
`files` map is not always empty; and a file may be moved back out to the root, so
`PUT` tells an absent `dir` from one explicitly `null`.

Both spaces answer the same shape, so one client reads them the same way. Ids come from
different spaces, so an id from one is not found in the other.

A file carries its own header, holding the name it is shown under and the status and
summary that go with it. `/contents` is the prose under that header: a read leaves the
header out, and a write keeps the one on disk.

Renaming a file changes its id. A client holding the old one gets a 404 and refetches
the tree, which is what it would do after any other change it did not make itself.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, StringConstraints

from .deps import CurrentUser, SettingsDep
from .fs import MAX_FILE_BYTES, MAX_NAME_LENGTH, MAX_SUMMARY_LENGTH
from .notes import NotesScanner
from .storage import Scanner

stories_router = APIRouter(prefix="/stories", tags=["stories"])
notes_router = APIRouter(prefix="/notes", tags=["notes"])

Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NAME_LENGTH),
]
Summary = Annotated[str, StringConstraints(max_length=MAX_SUMMARY_LENGTH)]


def get_scanner(request: Request, settings: SettingsDep) -> Scanner:
    """One scanner per application, so its scan outlives a request."""
    if request.app.state.scanner is None:
        request.app.state.scanner = Scanner(settings.data_root)
    return request.app.state.scanner


def get_notes(request: Request, settings: SettingsDep) -> NotesScanner:
    """One scanner per application for the other root, held the same way."""
    if request.app.state.notes_scanner is None:
        request.app.state.notes_scanner = NotesScanner(settings.files_root)
    return request.app.state.notes_scanner


ScannerDep = Annotated[Scanner, Depends(get_scanner)]
NotesDep = Annotated[NotesScanner, Depends(get_notes)]


class DirCreate(BaseModel):
    """A story or a folder to create."""

    name: Name
    summary: Summary | None = None


class DirUpdate(BaseModel):
    """What to change about one. An absent field is left as it is."""

    name: Name | None = None
    summary: Summary | None = None


class FileCreate(BaseModel):
    """A file to create, and where to create it."""

    #: The story or folder to create it in. Named `dir` by the clients. Under
    #: `/api/notes` it may be `null`, which is the root.
    dir: str | None = None
    name: Name


class FileUpdate(BaseModel):
    """What to change about a file. An absent field is left as it is."""

    name: Name | None = None
    #: Where to move the file to. A chapter always belongs to a story, so there `null`
    #: leaves it where it is; a note may be moved to the root, and there `null` is the
    #: root and leaving the field out is what leaves the note where it is.
    dir: str | None = None


# --- the stories ------------------------------------------------------------


@stories_router.get("/tree")
def tree(user: CurrentUser, scanner: ScannerDep) -> dict:
    """Every story, keyed by id.

    `files` holds the files that belong to no story, of which there are none here: a
    chapter lives in a story's directory. It is sent so a client can read this tree and
    the other one the same way.
    """
    return {
        "user": user.email,
        "files": {},
        "dirs": {
            story.id: {"name": story.name, "summary": story.summary}
            for story in scanner.tree().stories.values()
        },
    }


@stories_router.get("/dir/{dir_id}")
def dir_info(dir_id: str, scanner: ScannerDep) -> dict:
    """One story, the chapters in it, and which of the two other views it can offer.

    `timeline` and `lorebook` say whether the files those views read are there, so a
    client knows which to offer without fetching either. Neither is read here.
    """
    story = scanner.story(dir_id)
    return {
        "id": story.id,
        "name": story.name,
        "summary": story.summary,
        "timeline": story.has_timeline,
        "lorebook": story.has_lorebook,
        "files": {chapter.id: {"name": chapter.name} for chapter in story.chapters},
    }


@stories_router.post("/dir", status_code=status.HTTP_201_CREATED)
def create_dir(body: DirCreate, scanner: ScannerDep) -> dict:
    """Creates a story directory, with a manifest and an empty chapters directory."""
    story = scanner.create_story(body.name, body.summary or "")
    return {"id": story.id, "name": story.name, "summary": story.summary}


@stories_router.put("/dir/{dir_id}")
def update_dir(dir_id: str, body: DirUpdate, scanner: ScannerDep) -> dict:
    """Retitles a story, or rewrites its synopsis. Its id does not change."""
    story = scanner.update_story(dir_id, name=body.name, summary=body.summary)
    return {"id": story.id, "name": story.name, "summary": story.summary}


@stories_router.delete("/dir/{dir_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dir(dir_id: str, scanner: ScannerDep) -> Response:
    """Deletes a story and its chapters, unless the directory holds anything else."""
    scanner.delete_story(dir_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@stories_router.post("/file", status_code=status.HTTP_201_CREATED)
def create_file(body: FileCreate, scanner: ScannerDep) -> dict:
    """Creates a chapter in a story."""
    if body.dir is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A chapter belongs to a story. Choose one, or create a story first.",
        )
    chapter = scanner.create_chapter(body.dir, body.name)
    return {"id": chapter.id, "name": chapter.name, "dir": chapter.story_id}


@stories_router.get("/file/{file_id}")
def file_info(file_id: str, scanner: ScannerDep) -> dict:
    """One chapter: the name it is shown under, and what its header says about it."""
    chapter = scanner.chapter(file_id)
    return {
        "id": chapter.id,
        "name": chapter.name,
        "status": chapter.status,
        "summary": chapter.summary,
    }


@stories_router.put("/file/{file_id}")
def update_file(file_id: str, body: FileUpdate, scanner: ScannerDep) -> dict:
    """Renames a chapter, or moves it to another story. Either changes its id."""
    chapter = scanner.update_chapter(file_id, name=body.name, story_id=body.dir)
    return {"id": chapter.id, "name": chapter.name, "dir": chapter.story_id}


@stories_router.delete("/file/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(file_id: str, scanner: ScannerDep) -> Response:
    """Deletes a chapter."""
    scanner.delete_chapter(file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@stories_router.get("/file/{file_id}/contents", response_class=PlainTextResponse)
def read_contents(file_id: str, scanner: ScannerDep) -> PlainTextResponse:
    """A chapter's prose, as `text/plain`, without its header."""
    return PlainTextResponse(scanner.read_chapter(file_id))


@stories_router.put("/file/{file_id}/contents", status_code=status.HTTP_204_NO_CONTENT)
async def write_contents(
    file_id: str, request: Request, scanner: ScannerDep
) -> Response:
    """Replaces a chapter's prose with the request body, keeping its header."""
    return await replace_contents(request, scanner.write_chapter, file_id)


# --- everything that is not a novel -----------------------------------------


@notes_router.get("/tree")
def notes_tree(user: CurrentUser, notes: NotesDep) -> dict:
    """Every folder, and every file sitting at the root, each keyed by id."""
    scan = notes.tree()
    return {
        "user": user.email,
        "files": {
            note.id: {"name": note.name}
            for note in scan.notes.values()
            if note.folder_id is None
        },
        "dirs": {
            folder.id: {"name": folder.name, "summary": folder.context}
            for folder in scan.folders.values()
        },
    }


@notes_router.get("/dir/{dir_id}")
def notes_dir_info(dir_id: str, notes: NotesDep) -> dict:
    """One folder, its context, and the files in it."""
    folder = notes.folder(dir_id)
    return {
        "id": folder.id,
        "name": folder.name,
        "summary": folder.context,
        "files": {note.id: {"name": note.name} for note in folder.notes},
    }


@notes_router.post("/dir", status_code=status.HTTP_201_CREATED)
def notes_create_dir(body: DirCreate, notes: NotesDep) -> dict:
    """Creates a folder. It gets a manifest only if there is something to put in it."""
    folder = notes.create_folder(body.name, body.summary or "")
    return {"id": folder.id, "name": folder.name, "summary": folder.context}


@notes_router.put("/dir/{dir_id}")
def notes_update_dir(dir_id: str, body: DirUpdate, notes: NotesDep) -> dict:
    """Renames a folder, or rewrites its context. Its id does not change."""
    folder = notes.update_folder(dir_id, name=body.name, context=body.summary)
    return {"id": folder.id, "name": folder.name, "summary": folder.context}


@notes_router.delete("/dir/{dir_id}", status_code=status.HTTP_204_NO_CONTENT)
def notes_delete_dir(dir_id: str, notes: NotesDep) -> Response:
    """Deletes a folder and the files in it, unless it holds anything else."""
    notes.delete_folder(dir_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@notes_router.post("/file", status_code=status.HTTP_201_CREATED)
def notes_create_file(body: FileCreate, notes: NotesDep) -> dict:
    """Creates a file in a folder, or at the root when `dir` is `null`."""
    note = notes.create_note(body.dir, body.name)
    return {"id": note.id, "name": note.name, "dir": note.folder_id}


@notes_router.get("/file/{file_id}")
def notes_file_info(file_id: str, notes: NotesDep) -> dict:
    """One file: the name it is shown under, and what its header says about it."""
    note = notes.note(file_id)
    return {
        "id": note.id,
        "name": note.name,
        "status": note.status,
        "summary": note.summary,
    }


@notes_router.put("/file/{file_id}")
def notes_update_file(file_id: str, body: FileUpdate, notes: NotesDep) -> dict:
    """Renames a file, moves it to a folder or back to the root, or both.

    An absent `dir` leaves the file where it is; `dir: null` moves it to the root.
    """
    folder_id = (
        body.dir if "dir" in body.model_fields_set else notes.note(file_id).folder_id
    )
    note = notes.update_note(file_id, name=body.name, folder_id=folder_id)
    return {"id": note.id, "name": note.name, "dir": note.folder_id}


@notes_router.delete("/file/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def notes_delete_file(file_id: str, notes: NotesDep) -> Response:
    """Deletes a file."""
    notes.delete_note(file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@notes_router.get("/file/{file_id}/contents", response_class=PlainTextResponse)
def notes_read_contents(file_id: str, notes: NotesDep) -> PlainTextResponse:
    """A file's prose, as `text/plain`, without its header."""
    return PlainTextResponse(notes.read_note(file_id))


@notes_router.put("/file/{file_id}/contents", status_code=status.HTTP_204_NO_CONTENT)
async def notes_write_contents(
    file_id: str, request: Request, notes: NotesDep
) -> Response:
    """Replaces a file's prose with the request body, keeping its header."""
    return await replace_contents(request, notes.write_note, file_id)


# --- reading a body ---------------------------------------------------------

TOO_LONG = f"A file must be at most {MAX_FILE_BYTES} bytes."


def too_long(content_length: str | None) -> bool:
    """Whether the declared length is over the cap, so the body is refused unread."""
    try:
        return int(content_length) > MAX_FILE_BYTES if content_length else False
    except ValueError:
        return False


async def replace_contents(
    request: Request, write: Callable[[str, str], None], file_id: str
) -> Response:
    """Reads the request body as text and hands it to `write`."""
    if too_long(request.headers.get("content-length")):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LONG)

    body = await request.body()
    if len(body) > MAX_FILE_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LONG)

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "A file must be UTF-8 text."
        ) from error

    # Off the event loop: this endpoint has to be async to read the raw body, and the
    # write would otherwise hold up every request being served alongside it.
    await run_in_threadpool(write, file_id, text)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
