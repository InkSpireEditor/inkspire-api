# -*- coding: utf-8 -*-
"""The two constructors, date normalisation, the layout pass, and rendering to a string."""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
import yaml

from timeline import DEFAULT_TEMPLATE, Timeline, parse_date, template_source


def test_from_dict_and_from_yaml_agree(data: dict, yaml_file: Path) -> None:
    """fromYAML is fromDict with a file read in front of it, so both must render alike."""
    assert Timeline.fromDict(data).render() == Timeline.fromYAML(yaml_file).render()


def test_from_dict_reads_the_whole_file(data: dict) -> None:
    timeline = Timeline.fromDict(data)
    assert timeline._title == "Example Timeline"
    assert sorted(timeline._characters) == ["alpha", "beta"]
    assert sorted(timeline._events) == ["first", "second", "third"]
    assert sorted(timeline._arcs) == ["opening"]


def test_optional_event_fields_have_defaults(data: dict) -> None:
    timeline = Timeline.fromDict(data)
    assert timeline._events["first"].href == ""
    assert timeline._events["first"].date == "2001-01-01"
    # 'second' sets both, and dateStyle changes how the date renders.
    assert timeline._events["second"].href == "https://example.com/"
    assert timeline._events["second"].date == "2001-02"


MINIMAL_YAML = """\
title: "Bare"
characters:
  alpha:
    name: "Jane Doe"
    color: "blue"
events:
  only:
    date: 2001-01-01
    description: "Only"
    characters: ["alpha"]
"""


def test_arcs_and_positions_are_optional() -> None:
    """Both keys are caught by `except KeyError` and default to empty."""
    timeline = Timeline.fromDict(yaml.safe_load(MINIMAL_YAML))
    assert timeline._arcs == {}
    assert timeline._positions == {}


def test_the_three_written_date_forms_all_load(data: dict) -> None:
    """YAML hands over a date for an unquoted date and a string for the other two forms."""
    assert isinstance(data["events"]["first"]["date"], datetime.date)
    assert isinstance(data["events"]["second"]["date"], str)
    assert isinstance(data["events"]["third"]["date"], str)  # unpadded day stays text

    events = Timeline.fromDict(data)._events
    assert events["first"]._date == datetime.datetime(2001, 1, 1)
    assert events["second"]._date == datetime.datetime(2001, 2, 1)
    assert events["third"]._date == datetime.datetime(2001, 3, 5)


def test_dates_are_all_datetimes_so_events_can_be_sorted(data: dict) -> None:
    """Mixing a date with a datetime raises TypeError when process() sorts the events."""
    assert all(
        type(event._date) is datetime.datetime
        for event in Timeline.fromDict(data)._events.values()
    )


def test_parse_date_accepts_a_datetime_unchanged() -> None:
    moment = datetime.datetime(2001, 1, 1, 12, 30)
    assert parse_date(moment) is moment


def test_parse_date_rejects_a_bare_year_with_an_explanation() -> None:
    """`date: 2001` is an int in YAML, and the message has to say what to write instead."""
    with pytest.raises(TypeError, match="dateStyle"):
        parse_date(2001)


def test_an_unparseable_date_string_still_raises() -> None:
    with pytest.raises(ValueError):
        parse_date("the third of March")


def test_note_is_carried_in_the_data_and_rendered_by_nothing(data: dict) -> None:
    """Recorded in §8: `note` is authored on real events and no template reads it."""
    assert data["events"]["second"]["note"]
    assert "no template renders yet" not in Timeline.fromDict(data).render()


def test_process_places_events_in_date_order(data: dict) -> None:
    timeline = Timeline.fromDict(data)
    timeline.process()
    first, second, third = (timeline._events[k] for k in ("first", "second", "third"))
    assert first.x1 < second.x1 < third.x1
    # y comes from the positions entry for the event's character combination.
    assert first.y1 == 0
    assert second.y1 == pytest.approx(5.0)


def test_process_is_idempotent(data: dict) -> None:
    """The second pass would look arcs up by a key already replaced with an event."""
    timeline = Timeline.fromDict(data)
    timeline.process()
    timeline.process()
    assert timeline._arcs["opening"].firstEvent is timeline._events["first"]


def test_a_character_combination_with_no_position_is_an_error(data: dict) -> None:
    del data["positions"]["both"]
    with pytest.raises(KeyError):
        Timeline.fromDict(data).process()


def test_render_returns_the_source_and_writes_nothing(data: dict, tmp_path: Path) -> None:
    before = set(tmp_path.iterdir())
    rendered = Timeline.fromDict(data).render()
    assert isinstance(rendered, str)
    assert "Jane Doe" in rendered
    assert "First event" in rendered
    assert set(tmp_path.iterdir()) == before


def test_the_default_template_ignores_the_title(data: dict) -> None:
    """Recorded, not fixed: `title` is required by fromDict and typst.j2 never emits it."""
    assert "Example Timeline" not in Timeline.fromDict(data).render()


def test_render_lays_out_when_the_caller_did_not(data: dict) -> None:
    assert Timeline.fromDict(data).render() == Timeline.fromDict(data).render()


def test_render_accepts_a_template_of_the_callers_own(data: dict, tmp_path: Path) -> None:
    path = tmp_path / "custom.j2"
    path.write_text("{{ title }} has {{ events | length }} events", encoding="utf-8")
    assert Timeline.fromDict(data).render(path) == "Example Timeline has 3 events"


def test_every_packaged_template_is_readable() -> None:
    """`latex.j2` and `canvas.j2` are stale (§8) but must still ship and be readable."""
    for name in (DEFAULT_TEMPLATE, "latex.j2", "canvas.j2"):
        assert template_source(name).strip()


def test_template_is_found_from_any_working_directory(data: dict, tmp_path, monkeypatch) -> None:
    """The old code read templates/typst.j2 relative to the process working directory."""
    monkeypatch.chdir(tmp_path)
    assert "Jane Doe" in Timeline.fromDict(data).render()


def test_a_template_path_on_disk_wins(tmp_path: Path) -> None:
    path = tmp_path / "custom.j2"
    path.write_text("just this", encoding="utf-8")
    assert template_source(path) == "just this"


def test_an_unknown_template_names_itself() -> None:
    with pytest.raises(FileNotFoundError, match="absent.j2"):
        template_source("absent.j2")
