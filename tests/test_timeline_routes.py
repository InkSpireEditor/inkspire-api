# -*- coding: utf-8 -*-
"""The timeline route: `GET /api/stories/dir/{id}/timeline`."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TIMELINE, make_story, make_timeline


@pytest.fixture
def story_with_timeline(data_root: Path) -> Path:
    """A story carrying the fixture timeline."""
    story = make_story(data_root, "example", title="Example Story")
    make_timeline(story)
    return story


def story_id(client: TestClient, name: str = "Example Story") -> str:
    """The id of the story shown under `name`."""
    dirs = client.get("/api/stories/tree").json()["dirs"]
    return next(one for one, dir_ in dirs.items() if dir_["name"] == name)


# --- what a story says it has -----------------------------------------------


def test_a_story_says_whether_it_has_a_timeline(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    """`dir/{id}` carries the two booleans, so a client need not ask to find out."""
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}").json()
    assert body["timeline"] is True
    assert body["lorebook"] is False


def test_a_story_with_nothing_beside_it_says_so(
    logged_in: TestClient, data_root: Path
) -> None:
    make_story(data_root, "bare", title="Bare Story")
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in, 'Bare Story')}").json()
    assert body["timeline"] is False
    assert body["lorebook"] is False


def test_a_timeline_written_after_the_first_scan_is_noticed(
    logged_in: TestClient, data_root: Path
) -> None:
    """Writing the first `timeline.yaml` changes no name and no path, so the stamp has
    to carry its presence or the held scan would keep saying there is none."""
    story = make_story(data_root, "example", title="Example Story")
    assert logged_in.get(f"/api/stories/dir/{story_id(logged_in)}").json()["timeline"] is False

    make_timeline(story)
    assert logged_in.get(f"/api/stories/dir/{story_id(logged_in)}").json()["timeline"] is True


# --- the response -----------------------------------------------------------


def test_the_timeline_carries_its_title_and_height(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline").json()
    assert body["title"] == "Example Timeline"
    assert body["maxHeight"] > 0
    assert body["widthStep"] == 1


def test_every_event_is_laid_out(logged_in: TestClient, story_with_timeline: Path) -> None:
    """Coordinates are what the client draws, so none may come back unset."""
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline").json()
    assert [event["key"] for event in body["events"]] == ["first", "second", "third"]
    for event in body["events"]:
        assert all(event[corner] is not None for corner in ("x1", "y1", "x2", "y2"))
        assert event["x2"] > event["x1"]
        assert event["y2"] > event["y1"]


def test_events_come_back_in_date_order(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    """The order is the response's only statement about when things happen: the `date`
    field is already formatted through `dateStyle` and cannot be compared."""
    document = copy.deepcopy(TIMELINE)
    document["events"]["first"]["date"] = "2001-12-01"
    make_timeline(story_with_timeline, document)

    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline").json()
    assert [event["key"] for event in body["events"]] == ["second", "third", "first"]


def test_a_date_is_formatted_by_its_own_datestyle(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline").json()
    dates = {event["key"]: event["date"] for event in body["events"]}
    assert dates == {"first": "2001-01-01", "second": "2001-02", "third": "2001-03-01"}


def test_a_character_carries_its_events_as_keys(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    """Keys rather than copies, so each event is described once, and in date order so
    that consecutive pairs are the arrows to draw."""
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline").json()
    characters = {one["key"]: one for one in body["characters"]}
    assert characters["alpha"]["events"] == ["first", "second"]
    assert characters["beta"]["events"] == ["second", "third"]
    assert characters["alpha"]["name"] == "Jane Doe"
    assert characters["alpha"]["color"] == "blue"


def test_an_arc_names_the_events_it_spans(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    """`process()` replaces an arc's stored keys with the events, so serialising has to
    take the keys back off them."""
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline").json()
    assert body["arcs"] == [
        {"name": "Opening", "firstEvent": "first", "lastEvent": "third"}
    ]


def test_an_event_keeps_its_link_and_its_characters(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline").json()
    second = next(one for one in body["events"] if one["key"] == "second")
    assert second["href"] == "https://example.com/"
    assert sorted(second["characters"]) == ["alpha", "beta"]
    assert second["description"] == "Second event"


def test_reading_the_timeline_writes_nothing(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    """The story directory is a git working tree, so a read must not dirty it."""
    before = sorted(path.name for path in story_with_timeline.iterdir())
    logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline")
    assert sorted(path.name for path in story_with_timeline.iterdir()) == before


# --- what answers what ------------------------------------------------------


def test_an_unknown_story_is_not_found(logged_in: TestClient, data_root: Path) -> None:
    response = logged_in.get("/api/stories/dir/0123456789abcdef/timeline")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_a_story_with_no_timeline_is_not_found(
    logged_in: TestClient, data_root: Path
) -> None:
    make_story(data_root, "bare", title="Bare Story")
    response = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in, 'Bare Story')}/timeline"
    )
    assert response.status_code == 404
    assert "timeline.yaml" in response.json()["message"]


def test_a_missing_position_is_the_writers_to_fix(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    """`process()` raises `KeyError` for a character combination with no position. The
    file is there and a person has to fix it, so it is a 422 rather than a 500."""
    document = copy.deepcopy(TIMELINE)
    del document["positions"]["both"]
    make_timeline(story_with_timeline, document)

    response = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline")
    assert response.status_code == 422
    assert "timeline.yaml" in response.json()["message"]


@pytest.mark.parametrize("missing", ["title", "characters", "events"])
def test_a_timeline_missing_what_it_needs_is_unprocessable(
    logged_in: TestClient, story_with_timeline: Path, missing: str
) -> None:
    document = copy.deepcopy(TIMELINE)
    del document[missing]
    make_timeline(story_with_timeline, document)

    response = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline")
    assert response.status_code == 422


def test_a_date_written_as_a_bare_year_is_unprocessable(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    """A bare year arrives as an int and a bare month as text; `parse_date` refuses
    both, pointing at `dateStyle`."""
    document = copy.deepcopy(TIMELINE)
    document["events"]["first"]["date"] = 2001
    make_timeline(story_with_timeline, document)

    response = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline")
    assert response.status_code == 422
    assert "dateStyle" in response.json()["message"]


def test_the_route_refuses_a_request_with_no_token(
    client: TestClient, story_with_timeline: Path
) -> None:
    assert client.get("/api/stories/dir/anything/timeline").status_code == 401


# --- the held build ---------------------------------------------------------


def test_an_edited_timeline_is_read_again(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    """The build is held against the file's mtime, so an edit outside the app shows."""
    first = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline").json()
    assert first["title"] == "Example Timeline"

    document = copy.deepcopy(TIMELINE)
    document["title"] = "Retitled"
    make_timeline(story_with_timeline, document)

    second = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/timeline").json()
    assert second["title"] == "Retitled"


def test_two_reads_of_an_unchanged_timeline_agree(
    logged_in: TestClient, story_with_timeline: Path
) -> None:
    """The second read comes from the held build, and must not differ from the first —
    `process()` is idempotent and the coordinates it computed are kept."""
    url = f"/api/stories/dir/{story_id(logged_in)}/timeline"
    assert logged_in.get(url).json() == logged_in.get(url).json()
