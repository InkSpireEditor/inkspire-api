"""Generated SHACL validation passes on the fixture and catches broken references."""

from __future__ import annotations

from rdflib import Graph

from lorebook.ontology import SDO, Vocabulary
from lorebook.validation import validate


def test_canon_conforms(graph: Graph, vocab: Vocabulary) -> None:
    conforms, report = validate(graph, vocab)
    assert conforms, report


def test_dangling_mentor_reference_fails(graph: Graph, vocab: Vocabulary) -> None:
    # Point a mentorOf edge at an id that has no typed node in the graph.
    graph.add((vocab.iri("Alden"), vocab.object_properties["mentorOf"], vocab.iri("NoSuchPerson")))
    conforms, report = validate(graph, vocab)
    assert not conforms
    assert "mentorOf" in report


def test_missing_name_fails(graph: Graph, vocab: Vocabulary) -> None:
    # Remove Bryn's name to violate the minCount constraint.
    graph.remove((vocab.iri("Bryn"), SDO.name, None))
    conforms, _report = validate(graph, vocab)
    assert not conforms


def test_mistyped_relation_fails(graph: Graph, vocab: Vocabulary) -> None:
    # hasSkill pointed at a Location: right predicate, wrong target class.
    graph.add((vocab.iri("Alden"), vocab.object_properties["hasSkill"], vocab.iri("Northgate")))
    conforms, report = validate(graph, vocab)
    assert not conforms
    assert "Skill" in report
