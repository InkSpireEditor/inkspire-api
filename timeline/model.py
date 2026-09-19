# -*- coding: utf-8 -*-
"""The timeline model: characters, events, arcs, and the layout that places them.

A timeline is built either from a YAML file (:meth:`Timeline.fromYAML`) or from an
already-parsed mapping (:meth:`Timeline.fromDict`), laid out by :meth:`Timeline.process`
and rendered to source by :meth:`Timeline.render`, which returns a string and writes
nothing. The caller decides what to do with it.
"""

import datetime
from importlib import resources
from pathlib import Path

import jinja2
import yaml

#: Template used when the caller names none. Shipped inside the package.
DEFAULT_TEMPLATE = "typst.j2"

#: Written form a date is read from when it arrives as text rather than as a date.
DATE_FORMAT = "%Y-%m-%d"


def parse_date(value):
    """Normalises an authored date to a ``datetime``.

    YAML reads an unquoted ``2019-10-03`` as a ``datetime.date`` and a quoted one as a
    string, and a day written without its leading zero stays a string either way. All of
    them become a ``datetime`` here, because events are sorted against each other and
    comparing a ``date`` to a ``datetime`` raises ``TypeError``.

    How much of the date is *shown* is a separate matter, set per event by ``dateStyle``.
    """
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, datetime.date):  # datetime is a date, so this order matters
        return datetime.datetime(value.year, value.month, value.day)
    if isinstance(value, str):
        return datetime.datetime.strptime(value, DATE_FORMAT)
    raise TypeError(
        f"date must be a date or a {DATE_FORMAT!r} string, got {type(value).__name__} "
        f"({value!r}). A bare year reads as a number and a bare month as text, so write "
        "the full date and hide the part you do not want shown with dateStyle."
    )


def template_source(template):
    """The Jinja source for ``template``.

    A path to an existing file is read from disk; anything else is looked up among the
    templates shipped in ``timeline/templates``. Resolving through ``importlib.resources``
    rather than a relative path is what lets the command run from any directory.
    """
    path = Path(template)
    if path.is_file():
        return path.read_text(encoding="utf-8")
    resource = resources.files("timeline").joinpath("templates", str(template))
    if not resource.is_file():
        raise FileNotFoundError(
            f"No template {template!r}: not a file on disk, and not shipped in timeline/templates."
        )
    return resource.read_text(encoding="utf-8")


class Character(object):
    """Class representing a character."""
    def __init__(self, key, name, color, events):
        self._key = key
        self._name = name
        self._color = color
        self._events = events

    @property
    def key(self):
        """Returns the key of the character."""
        return self._key

    @property
    def name(self):
        """Returns the name of the character."""
        return self._name

    @property
    def color(self):
        """Returns the color of the character."""
        return self._color

    @property
    def events(self):
        """Returns the events associated with the character."""
        return self._events

    @events.setter
    def events(self, events):
        """Sets the events associated with the character."""
        self._events = events

    def __repr__(self):
        return f"Character<{hex(id(self))}>(name='{self.name}', color='{self.color}', events_count={len(self.events)})"


class Event(object):
    """Class representing events in the timeline."""

    _WIDTH_FACTOR = 1 / 4

    def __init__(self, key, date, characters, description, dateStyle, href, coords=None):
        self._key = key
        self._date = date
        self._characters = characters
        self._description = description
        self._coords = {
            "x1": None,
            "y1": None,
            "x2": None,
            "y2": None
        } if coords is None else coords
        self._dateStyle = dateStyle
        self._href = href

    @property
    def key(self):
        """Returns the key of the event."""
        return self._key

    @property
    def date(self):
        """Returns the date associated with the event."""
        return self._date.strftime(self._dateStyle)

    @property
    def characters(self):
        """Returns the characters associated with the event."""
        return self._characters

    @property
    def description(self):
        """Returns the description associated with the event."""
        return self._description

    @property
    def x1(self):
        """Returns the horizontal coordinate (time)."""
        return self._coords["x1"]

    @x1.setter
    def x1(self, x1):
        self._coords["x1"] = x1

    @property
    def y1(self):
        """Returns the vertical coordinate (characters)."""
        return self._coords["y1"]

    @y1.setter
    def y1(self, y1):
        self._coords["y1"] = y1

    @property
    def x2(self):
        """Returns the horizontal coordinate (time)."""
        return self._coords["x2"]

    @x2.setter
    def x2(self, x2):
        self._coords["x2"] = x2

    @property
    def y2(self):
        """Returns the vertical coordinate (characters)."""
        return self._coords["y2"]

    @y2.setter
    def y2(self, y2):
        self._coords["y2"] = y2

    @property
    def dateStyle(self):
        """Returns the date format."""
        return self._dateStyle

    @property
    def href(self):
        """Returns the hyperlink associated with the event."""
        return self._href

    @property
    def width(self):
        """Returns the width needed by the event."""
        return len(self._description) * self._WIDTH_FACTOR

    def __repr__(self):
        return (
            f"Event<{hex(id(self))}>(key='{self._key}', date='{self._date.strftime(self._dateStyle)}', "
            f"characters={self._characters!r}, description={self._description!r}, "
            f"href={self._href!r}, x1={self.x1}, y1={self.y1}, x2={self.x2}, y2={self.y2}, "
            f"dateStyle='{self._dateStyle!r}', width={self.width})"
        )


class Arc(object):
    """Arc containing several consecutive events."""
    def __init__(self, firstEvent, lastEvent, name):
        self._firstEvent = firstEvent
        self._lastEvent = lastEvent
        self._name = name

    @property
    def firstEvent(self):
        """Returns the first event of the arc."""
        return self._firstEvent

    @property
    def lastEvent(self):
        """Returns the last event of the arc."""
        return self._lastEvent

    @property
    def name(self):
        """Returns the name of the arc."""
        return self._name

    def __repr__(self):
        return (
            f"Arc<{hex(id(self))}>(firstEvent={self.firstEvent!r}, "
            f"lastEvent={self.lastEvent!r}, name='{self.name}')"
        )


class Timeline(object):
    """Class representing a timeline as an ordered set of events."""

    def __init__(
        self,
        title,
        characters,
        events,
        arcs=None,
        positions=None,
        widthStep=1,
        heightspan=1.5,
        heightStep=1,
        archeight=150,
    ):
        self._title = title
        self._characters = characters
        self._events = events
        self._widthStep = widthStep
        self._heightspan = heightspan
        self._heightStep = heightStep
        self._arcHeight = archeight
        # Both are keyed maps -- `process()` calls `.values()` on the arcs and looks a
        # position up by its key -- so the empty default is a dict, not a list.
        self._arcs = {} if arcs is None else arcs
        self._positions = {} if positions is None else positions
        self._processed = False
        # Set by `process()`, and declared here so reading the height before laying the
        # timeline out answers None rather than raising.
        self._maxHeight = None

    def __repr__(self):
        return (
            f"Timeline<{hex(id(self))}>(title='{self._title}', "
            f"characters_count={len(self._characters)}, "
            f"events_count={len(self._events)}, "
            f"arcs_count={len(self._arcs)})"
        )

    @property
    def title(self):
        """Returns the title of the timeline."""
        return self._title

    @property
    def characters(self):
        """Returns the characters, keyed as they were authored."""
        return self._characters

    @property
    def arcs(self):
        """Returns the arcs, keyed as they were authored.

        After `process()` an arc's ends are the events themselves rather than the keys
        they were written as.
        """
        return self._arcs

    @property
    def events(self):
        """Returns the events in date order.

        The order is the one everything else assumes: `process()` places them along x
        in it, and a character's own event list is in it too.
        """
        return tuple(sorted(self._events.values(), key=lambda e: e._date))

    @property
    def maxHeight(self):
        """Returns the height the drawing needs. Set by `process()`."""
        return self._maxHeight

    @property
    def widthStep(self):
        """Returns the horizontal gap left between two consecutive events."""
        return self._widthStep

    def process(self):
        """Returns (x,y) positions for each event to draw.

        Running twice would fail, because the second pass looks arcs up by an event key
        that the first pass already replaced with the event itself. The guard makes the
        call idempotent so a caller can render without tracking whether it laid out yet.
        """
        if self._processed:
            return
        events = sorted(self._events.items(), key=lambda e: e[1]._date)
        lastX = 0
        for _, e in events:
            # First handle x position.
            e.x1 = lastX + self._widthStep
            e.x2 = e.x1 + e.width

            # Keep track of current x position.
            lastX = e.x2

            # Handle y position.
            characters = e.characters
            if characters not in self._positions:
                raise KeyError(
                    "Position for character combination {} not defined in project file".format(list(characters))
                )
            position = self._positions[characters]
            e.y1 = (self._heightspan + self._heightStep) * position

            # Adjust node height.
            e.y2 = e.y1 + self._heightspan

        # Max height requires to sort events by height and take the last.
        self._maxHeight = sorted(self._events.values(), key=lambda e: e.y2)[-1].y2 + self._heightspan

        # Setup events by characters to draw arrow.
        for character in self._characters.values():
            character.events = []
            for event in sorted(self._events.values(), key=lambda e: e._date):
                if character.key in event.characters:
                    character.events += [event]

        # Sort events for each character.
        for character in self._characters.values():
            character.events = sorted(character.events, key=lambda e: e._date)

        # Setup arcs with actual events.
        for arc in self._arcs.values():
            arc._firstEvent = self._events[arc._firstEvent]
            arc._lastEvent = self._events[arc._lastEvent]

        self._processed = True

    def render(self, template=DEFAULT_TEMPLATE):
        """Renders the timeline and returns the generated source.

        Lays the timeline out first if that has not happened yet, and writes nothing --
        the caller decides whether the result goes to a file, to stdout, or into an HTTP
        response.
        """
        self.process()
        return jinja2.Template(template_source(template)).render(
            title=self.title,
            events=self.events,
            maxHeight=self.maxHeight,
            characters=self.characters,
            widthstep=self.widthStep,
            arcs=self.arcs.values(),
        )

    @classmethod
    def fromYAML(cls, filename):
        """Imports timeline from a YAML configuration file."""
        with open(filename, "r", encoding="utf-8") as handle:
            return cls.fromDict(yaml.safe_load(handle))

    @classmethod
    def fromDict(cls, res):
        """Imports timeline from an already-parsed mapping.

        The same shape :meth:`fromYAML` parses out of a file. This is the constructor to
        use when the data came from somewhere other than a YAML file on disk.
        """
        title = res["title"]

        characters = {
            k: Character(
                k,
                v["name"],
                v["color"],
                [],
            )
            for k,
            v in res["characters"].items()
        }

        try:
            # /!\ All characters must have a position set /!\
            positions = {
                tuple(sorted(v["characters"])): v["position"]
                for v in res["positions"].values()
            }
        except KeyError:
            positions = {}

        events = {
            k:
                Event(
                    k,
                    parse_date(v["date"]),
                    tuple(sorted(v["characters"])),
                    v["description"],
                    dateStyle=v["dateStyle"] if "dateStyle" in v.keys() else "%Y-%m-%d",
                    href=v["href"] if "href" in v.keys() else "",
                )
            for k,
            v in res["events"].items()
        }
        try:
            arcs = {
                k: Arc(v["firstEvent"],
                       v["lastEvent"],
                       v["name"])
                for k,
                v in res["arcs"].items()
            }
        except KeyError:
            arcs = {}

        return cls(title, characters, events, arcs, positions)
