"""Render each character/place in the graph back out to a Markdown sheet.

Structured relations are read as graph edges and resolved to human-readable labels;
prose sections are read from their literal properties. The result reproduces the
11-section character/place template, fully regenerated from the canonical graph.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from importlib import resources
from jinja2 import Environment, Template
from rdflib import Graph, URIRef
from rdflib.term import Node

from .ontology import RDF, SDO, Vocabulary

# (Markdown heading, YAML/section key) in template order. Section 1 (Basic
# Information) is rendered from the structured bullet block, not a prose section.
SECTION_ORDER: list[tuple[str, str]] = [
    ("Personality and Traits", "personality"),
    ("Backstory", "backstory"),
    ("Role in the Story", "role"),
    ("Abilities and Skills", "abilities"),
    ("Relationships", "relationships"),
    ("World Interaction", "world"),
    ("Quotes and Voice", "voice"),
    ("Miscellaneous", "misc"),
    ("Visuals and References", "visuals"),
    ("Notes for Further Development", "notes"),
]

LOCATION_SECTION_ORDER: list[tuple[str, str]] = [
    ("History and Origin", "history"),
    ("Purpose and Function", "purpose"),
    ("Inhabitants and Culture", "inhabitants"),
    ("Geography and Layout", "geography"),
    ("Politics and Power", "politics"),
    ("Threats and Challenges", "threats"),
    ("Lore and Mysteries", "lore"),
    ("Interaction with the Story", "narrative"),
    ("Visuals and Symbols", "visuals"),
    ("Notes for Further Development", "notes"),
]


@dataclass(frozen=True)
class _Kind:
    """One exportable node kind: its class, template, and section layout."""

    class_of: str  # attribute name on Vocabulary, e.g. "character_class"
    template_name: str
    section_order: list[tuple[str, str]]


_KINDS = [
    _Kind("character_class", "character.md.j2", SECTION_ORDER),
    _Kind("location_class", "location.md.j2", LOCATION_SECTION_ORDER),
]


def _label(graph: Graph, node: Node) -> str:
    """Display label for an entity node: its schema:name, else its IRI fragment."""
    name = graph.value(node, SDO.name)
    if name is not None:
        return str(name)
    if isinstance(node, URIRef):
        return str(node).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
    return str(node)


def _template(name: str) -> Template:
    text = resources.files("lorebook").joinpath("templates", name).read_text("utf-8")
    return Environment().from_string(text)


def node_context(
    graph: Graph, subject: URIRef, vocab: Vocabulary, section_order: list[tuple[str, str]]
) -> dict:
    """Assemble the render context for one subject (character or place)."""
    context: dict = {"section_order": section_order}

    # Scalars.
    for field, predicate in vocab.scalar_properties.items():
        value = graph.value(subject, predicate)
        context[field] = str(value) if value is not None else None

    # Nicknames.
    context["aka"] = sorted(str(o) for o in graph.objects(subject, vocab.aka))

    # Object relations -> sorted lists of labels.
    for field, predicate in vocab.object_properties.items():
        labels = sorted(_label(graph, o) for o in graph.objects(subject, predicate))
        context[field] = labels

    # Prose sections.
    context["sections"] = {
        key: str(value)
        for _heading, key in section_order
        if (value := graph.value(subject, vocab.section_property(key))) is not None
    }
    return context


def character_context(graph: Graph, subject: URIRef, vocab: Vocabulary) -> dict:
    """Assemble the render context for one character subject."""
    return node_context(graph, subject, vocab, SECTION_ORDER)


def section_order_for(graph: Graph, subject: URIRef, vocab: Vocabulary) -> list[tuple[str, str]]:
    """The section layout ``subject`` is written under, empty if it has none.

    Which one applies is decided by ``rdf:type``, and subclass types are materialised
    by :mod:`lorebook.authoring`, so a ``SecondaryCharacter`` carries ``Character`` too
    and matches the character layout without any inference. A subject that is neither a
    character nor a location gets no sections, which is why :func:`export_all` writes it
    no sheet.
    """
    types = set(graph.objects(subject, RDF.type))
    for kind in _KINDS:
        if getattr(vocab, kind.class_of) in types:
            return kind.section_order
    return []


def export_all(graph: Graph, out_dir: str | Path, vocab: Vocabulary) -> list[Path]:
    """Render every Character/Location to ``<out_dir>/<id>.md``; return the paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    for kind in _KINDS:
        class_iri = getattr(vocab, kind.class_of)
        template = _template(kind.template_name)
        for subject in sorted(graph.subjects(RDF.type, class_iri), key=str):
            assert isinstance(subject, URIRef)
            context = node_context(graph, subject, vocab, kind.section_order)
            # Only fully-authored nodes (those carrying prose sections) get a
            # sheet; referenced stubs are skipped.
            if not context["sections"]:
                continue
            markdown = template.render(**context)
            local_id = str(subject).rsplit("#", 1)[-1]
            path = out / f"{local_id}.md"
            path.write_text(markdown, encoding="utf-8")
            written.append(path)
    return sorted(written)
