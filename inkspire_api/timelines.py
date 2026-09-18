# -*- coding: utf-8 -*-
"""A story's timeline, laid out and served as JSON.

    GET /api/stories/dir/{id}/timeline

The `timeline` package does the work. This reads the story's `timeline.yaml`, calls
`process()` and turns what it computed into JSON: a coordinate box per event, each
character's events in date order, and the arcs. The browser draws those coordinates and
lays nothing out itself, so the on-screen timeline and the Typst the CLI renders come
from one layout pass and cannot disagree.

`timeline.yaml` is authored by hand. Nothing here writes to it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from timeline import Timeline

from .deps import SettingsDep
from .fs import HeldPerKey, Malformed, NotFound, mtime
from .files import ScannerDep
from .storage import STORIES, TIMELINE, Scanner

router = APIRouter(prefix="/stories/dir", tags=["timeline"])


def timeline_path(scanner: Scanner, story_id: str) -> Path:
    """Where the story's timeline is, whether or not anything is there."""
    story = scanner.story(story_id)
    return scanner.path(story.relpath / TIMELINE)


def load(scanner: Scanner, story_id: str) -> dict:
    """The story's timeline, parsed, laid out and ready to serialise.

    Raises `NotFound` when the story has no timeline at all, and `Malformed` when it
    has one that cannot be read as a timeline.
    """
    path = timeline_path(scanner, story_id)
    if not path.is_file():
        raise NotFound(
            f"This story has no {TIMELINE}. Write one beside its {STORIES} directory "
            "to plan its events."
        )
    try:
        timeline = Timeline.fromYAML(path)
        timeline.process()
    except KeyError as exc:
        # `process()` raises this for a character combination with no `positions`
        # entry, and `fromDict` for a missing `title`, `characters` or `events`.
        raise Malformed(f"{TIMELINE} is missing {exc}.") from exc
    except (TypeError, ValueError) as exc:
        # An unparseable date, or a date written as a bare year or month.
        raise Malformed(f"{TIMELINE} cannot be read: {exc}") from exc
    return as_json(timeline)


def as_json(timeline: Timeline) -> dict:
    """What `process()` computed, as the response body.

    A character's events and an arc's ends are event keys rather than copies of the
    events, so each event is described once. Consecutive keys in a character's list are
    the pairs to draw an arrow between.
    """
    return {
        "title": timeline.title,
        "maxHeight": timeline.maxHeight,
        "widthStep": timeline.widthStep,
        "characters": [
            {
                "key": character.key,
                "name": character.name,
                "color": character.color,
                "events": [event.key for event in character.events],
            }
            for character in timeline.characters.values()
        ],
        "events": [
            {
                "key": event.key,
                # Already formatted through this event's `dateStyle`, so it is what to
                # show. The order of this list is what to sort by.
                "date": event.date,
                "description": event.description,
                "characters": list(event.characters),
                "href": event.href,
                "x1": event.x1,
                "y1": event.y1,
                "x2": event.x2,
                "y2": event.y2,
            }
            for event in timeline.events
        ],
        "arcs": [
            {
                "name": arc.name,
                "firstEvent": arc.firstEvent.key,
                "lastEvent": arc.lastEvent.key,
            }
            for arc in timeline.arcs.values()
        ],
    }


def get_timelines(request: Request, scanner: ScannerDep) -> HeldPerKey[dict]:
    """One cache per application, holding each story's laid-out timeline.

    Held against the timeline file's own mtime: parsing and laying out a hundred events
    on every request would be work repeated for a file almost nobody is editing.
    """
    if request.app.state.timelines is None:
        request.app.state.timelines = HeldPerKey(
            lambda story_id: load(scanner, story_id),
            lambda story_id: (mtime(timeline_path(scanner, story_id)),),
        )
    return request.app.state.timelines


TimelinesDep = Annotated[HeldPerKey[dict], Depends(get_timelines)]


@router.get("/{dir_id}/timeline")
def story_timeline(dir_id: str, timelines: TimelinesDep) -> dict:
    """The story's events, laid out: coordinates, characters and arcs."""
    return timelines.get(dir_id)
