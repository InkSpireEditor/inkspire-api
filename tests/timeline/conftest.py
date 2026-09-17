# -*- coding: utf-8 -*-
"""A small made-up timeline, used by every test.

Deliberately not `examples/input.yaml` and not any real project file: the fixture uses
placeholder names so the suite stays independent of authored story content.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

#: Each event deliberately writes its date differently, because YAML reads the three forms
#: as three different types: a `date`, a string, and a string again for the unpadded day.
TIMELINE_YAML = """\
title: "Example Timeline"

characters:
  alpha:
    name: "Jane Doe"
    color: "blue"

  beta:
    name: "John Smith"
    color: "red"

positions:
  alpha:
    characters: ["alpha"]
    position: 0

  beta:
    characters: ["beta"]
    position: 1

  both:
    characters: ["alpha", "beta"]
    position: 2

events:
  first:
    date: 2001-01-01
    description: "First event"
    characters: ["alpha"]

  second:
    date: "2001-02-01"
    description: "Second event"
    characters: ["alpha", "beta"]
    dateStyle: "%Y-%m"
    href: "https://example.com/"
    note: "A note, which no template renders yet."

  third:
    date: 2001-3-5
    description: "Third event"
    characters: ["beta"]

arcs:
  opening:
    firstEvent: "first"
    lastEvent: "third"
    name: "Opening"
"""


@pytest.fixture()
def data() -> dict:
    """The fixture timeline as a parsed mapping, ready for `Timeline.fromDict`."""
    return yaml.safe_load(TIMELINE_YAML)


@pytest.fixture()
def yaml_file(tmp_path: Path) -> Path:
    """The fixture timeline written to a file, ready for `Timeline.fromYAML`."""
    path = tmp_path / "example.yaml"
    path.write_text(TIMELINE_YAML, encoding="utf-8")
    return path
