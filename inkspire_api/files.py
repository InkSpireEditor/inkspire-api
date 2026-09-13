# -*- coding: utf-8 -*-
"""File and directory routes, served from the stories on disk.

A story is a directory and its chapters are that directory's files:

    GET    /api/tree                  every story, and the loose files at the root
    GET    /api/dir/{id}              one story, and the chapters in it
    POST   /api/dir                   create a story
    PUT    /api/dir/{id}              retitle a story, or rewrite its synopsis
    DELETE /api/dir/{id}              delete a story and its chapters
    POST   /api/file                  create a chapter in a story
    GET    /api/file/{id}             one chapter's id and name
    PUT    /api/file/{id}             rename a chapter, or move it to another story
    DELETE /api/file/{id}             delete a chapter
    GET    /api/file/{id}/contents    the chapter, as text/plain
    PUT    /api/file/{id}/contents    replace the chapter with the request body

A chapter belongs to a story, so `files` at the root of the tree is always empty and a
chapter cannot be created outside one.

Renaming a chapter changes its id. A client holding the old one gets a 404 and refetches
the tree, which is what it would do after any other change it did not make itself.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, StringConstraints

from .deps import CurrentUser, SettingsDep
from .storage import (
    MAX_CHAPTER_BYTES,
    MAX_NAME_LENGTH,
    MAX_SUMMARY_LENGTH,
    Scanner,
)

router = APIRouter(tags=["files"])

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


ScannerDep = Annotated[Scanner, Depends(get_scanner)]


class DirCreate(BaseModel):
    name: Name
    summary: Summary | None = None


class DirUpdate(BaseModel):
    name: Name | None = None
    summary: Summary | None = None


class FileCreate(BaseModel):
    #: The story to create the chapter in. Named `dir` by the clients.
    dir: str | None = None
    name: Name


class FileUpdate(BaseModel):
    name: Name | None = None
    #: A story to move the chapter to. A chapter always belongs to one, so there is
    #: nowhere to move it out to and `null` leaves it where it is.
    dir: str | None = None


@router.get("/tree")
def tree(user: CurrentUser, scanner: ScannerDep) -> dict:
    """Every story, keyed by id.

    `files` holds the files that belong to no story, of which there are none: a
    chapter lives in a story's directory. It is sent so a client can read the two maps
    the same way.
    """
    return {
        "user": user.email,
        "files": {},
        "dirs": {
            story.id: {"name": story.name, "summary": story.summary}
            for story in scanner.tree().stories.values()
        },
    }


@router.get("/dir/{dir_id}")
def dir_info(dir_id: str, scanner: ScannerDep) -> dict:
    story = scanner.story(dir_id)
    return {
        "id": story.id,
        "name": story.name,
        "summary": story.summary,
        "files": {chapter.id: {"name": chapter.name} for chapter in story.chapters},
    }


@router.post("/dir", status_code=status.HTTP_201_CREATED)
def create_dir(body: DirCreate, scanner: ScannerDep) -> dict:
    story = scanner.create_story(body.name, body.summary or "")
    return {"id": story.id, "name": story.name, "summary": story.summary}


@router.put("/dir/{dir_id}")
def update_dir(dir_id: str, body: DirUpdate, scanner: ScannerDep) -> dict:
    story = scanner.update_story(dir_id, name=body.name, summary=body.summary)
    return {"id": story.id, "name": story.name, "summary": story.summary}


@router.delete("/dir/{dir_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dir(dir_id: str, scanner: ScannerDep) -> Response:
    scanner.delete_story(dir_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/file", status_code=status.HTTP_201_CREATED)
def create_file(body: FileCreate, scanner: ScannerDep) -> dict:
    if body.dir is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A chapter belongs to a story. Choose one, or create a story first.",
        )
    chapter = scanner.create_chapter(body.dir, body.name)
    return {"id": chapter.id, "name": chapter.name, "dir": chapter.story_id}


@router.get("/file/{file_id}")
def file_info(file_id: str, scanner: ScannerDep) -> dict:
    chapter = scanner.chapter(file_id)
    return {"id": chapter.id, "name": chapter.name}


@router.put("/file/{file_id}")
def update_file(file_id: str, body: FileUpdate, scanner: ScannerDep) -> dict:
    chapter = scanner.update_chapter(file_id, name=body.name, story_id=body.dir)
    return {"id": chapter.id, "name": chapter.name, "dir": chapter.story_id}


@router.delete("/file/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_file(file_id: str, scanner: ScannerDep) -> Response:
    scanner.delete_chapter(file_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/file/{file_id}/contents", response_class=PlainTextResponse)
def read_contents(file_id: str, scanner: ScannerDep) -> PlainTextResponse:
    return PlainTextResponse(scanner.read_chapter(file_id))


@router.put("/file/{file_id}/contents", status_code=status.HTTP_204_NO_CONTENT)
async def write_contents(
    file_id: str, request: Request, scanner: ScannerDep
) -> Response:
    """Replaces a chapter with the request body, which is the text itself."""
    if too_long(request.headers.get("content-length")):
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LONG)

    body = await request.body()
    if len(body) > MAX_CHAPTER_BYTES:
        raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, TOO_LONG)

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "A chapter must be UTF-8 text."
        ) from error

    # Off the event loop: this endpoint has to be async to read the raw body, and the
    # write would otherwise hold up every request being served alongside it.
    await run_in_threadpool(scanner.write_chapter, file_id, text)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


TOO_LONG = f"A chapter must be at most {MAX_CHAPTER_BYTES} bytes."


def too_long(content_length: str | None) -> bool:
    """Whether the declared length is over the cap, so the body is refused unread."""
    try:
        return int(content_length) > MAX_CHAPTER_BYTES if content_length else False
    except ValueError:
        return False
