"""The interactive viewer: graph -> node/link data + a safe, self-contained page."""

from __future__ import annotations

import json

from rdflib import Graph, Literal

from lorebook.ontology import CORE, RDF, SDO, Vocabulary
from lorebook import view as view_mod


def test_build_view_data_shapes_nodes_and_links(graph: Graph, vocab: Vocabulary) -> None:
    data = view_mod.build_view_data(graph, vocab)
    by_id = {n["id"]: n for n in data["nodes"]}

    alden = by_id[str(vocab.iri("Alden"))]
    assert alden["label"] == "Alden"
    assert "Character" in alden["types"]
    # Prose sections are excluded; short scalars are kept as attrs.
    assert alden["attrs"].get("rank") == ["Master"]
    assert not any(k.startswith("section") for k in alden["attrs"])

    # Object properties become edges; rdf:type never does.
    labels = {(l["source"], l["label"], l["target"]) for l in data["links"]}
    assert (str(vocab.iri("Alden")), "mentorOf", str(vocab.iri("Bryn"))) in labels
    assert all(l["label"] != "type" for l in data["links"])

    # Degree counts every incident edge.
    assert alden["degree"] == sum(
        1 for l in data["links"] if str(vocab.iri("Alden")) in (l["source"], l["target"])
    )


def test_primary_type_is_the_most_specific_class(vocab: Vocabulary) -> None:
    """A subclass must not collapse into its parent, or the legend cannot filter it.

    Subclass types are materialized, so a SecondaryCharacter node carries
    Character and Entity too. The legend colours and toggles by `type`, so that
    has to be the deepest class -- otherwise secondary characters are
    indistinguishable from the main cast and cannot be hidden.
    """
    g = Graph()
    for cls in (CORE.SecondaryCharacter, CORE.Character, CORE.Entity):
        g.add((vocab.iri("Innkeep"), RDF.type, cls))
    g.add((vocab.iri("Innkeep"), SDO.name, Literal("Innkeep")))
    for cls in (CORE.Character, CORE.Entity):
        g.add((vocab.iri("Hero"), RDF.type, cls))
    g.add((vocab.iri("Hero"), SDO.name, Literal("Hero")))

    by_label = {n["label"]: n for n in view_mod.build_view_data(g, vocab)["nodes"]}
    assert by_label["Innkeep"]["type"] == "SecondaryCharacter"
    assert by_label["Hero"]["type"] == "Character"
    # The full list is still reported (tooltips), minus the uninformative root.
    assert by_label["Innkeep"]["types"] == ["Character", "SecondaryCharacter"]
    assert "Entity" not in by_label["Hero"]["types"]


def test_class_iris_are_not_nodes(graph: Graph, vocab: Vocabulary) -> None:
    data = view_mod.build_view_data(graph, vocab)
    ids = {n["id"] for n in data["nodes"]}
    assert str(vocab.character_class) not in ids  # only ever an rdf:type object
    assert str(vocab.iri("Alden")) in ids


def test_render_html_is_self_contained_and_safe(graph: Graph, vocab: Vocabulary) -> None:
    html = view_mod.render_html(graph, vocab, title="Lorebook")

    # Self-contained + offline: vendored engine inlined, no proxy, no CDN/fonts.
    assert "Version 1.51.4 force-graph" in html
    assert "cors-anywhere" not in html
    assert "https://cdn" not in html and "unpkg" not in html
    assert "googleapis" not in html
    assert "<script src=" not in html  # nothing loaded over the wire

    # Data is parsed, never eval'd, and cannot break out of the <script>.
    assert "JSON.parse(" in html
    assert "eval(" not in html
    assert "</script" not in json.dumps(view_mod.build_view_data(graph, vocab))


def test_embedded_json_neutralizes_markup_breakout() -> None:
    payload = view_mod._embed_json({"x": "</script><img src=x onerror=alert(1)>"})
    # The dangerous delimiters are escaped, but it stays valid JSON.
    assert "</script>" not in payload
    assert "\\u003c" in payload
    assert json.loads(payload) == {"x": "</script><img src=x onerror=alert(1)>"}


def test_malicious_label_does_not_inject_raw_html(vocab: Vocabulary) -> None:
    g = Graph()
    evil = "<img src=x onerror=alert(1)>"
    g.add((vocab.iri("Evil"), RDF.type, vocab.character_class))
    g.add((vocab.iri("Evil"), SDO.name, Literal(evil)))
    html = view_mod.render_html(g, vocab)

    # The label survives as (delimiter-escaped) JSON string data, but the raw
    # "<img ...>" tag is never emitted into markup; runtime esc() handles display.
    assert evil not in html
    assert "\\u003cimg src=x onerror=alert(1)\\u003e" in html


def test_legend_types_are_interactive_toggles(graph: Graph, vocab: Vocabulary) -> None:
    html = view_mod.render_html(graph, vocab)
    # The legend rows are real buttons that filter the graph by type.
    assert 'role", "button"' in html
    assert "hiddenTypes" in html and "visibleData" in html
    assert "legend-title" in html  # the "show all" reset affordance


def test_empty_graph_renders_without_error(vocab: Vocabulary) -> None:
    html = view_mod.render_html(Graph(), vocab)
    assert "no entities to display" in html
