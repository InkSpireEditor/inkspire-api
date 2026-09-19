"""YAML -> RDF mapping produces the expected edges and literals."""

from __future__ import annotations

from rdflib import Graph

from lorebook.ontology import RDF, SDO, Vocabulary


def test_mentor_edges_present(graph: Graph, vocab: Vocabulary) -> None:
    mentor_of = vocab.object_properties["mentorOf"]
    for student in ("Bryn", "Cora"):
        assert (vocab.iri("Alden"), mentor_of, vocab.iri(student)) in graph


def test_extension_edges_present(graph: Graph, vocab: Vocabulary) -> None:
    has_skill = vocab.object_properties["hasSkill"]
    assert (vocab.iri("Alden"), has_skill, vocab.iri("Firecraft")) in graph
    assert (vocab.iri("Alden"), has_skill, vocab.iri("Frostcraft")) in graph


def test_scalar_and_aka_literals(graph: Graph, vocab: Vocabulary) -> None:
    assert graph.value(vocab.iri("Alden"), SDO.name) is not None
    assert str(graph.value(vocab.iri("Alden"), vocab.scalar_properties["rank"])) == "Master"
    akas = {str(o) for o in graph.objects(vocab.iri("Alden"), vocab.aka)}
    assert "Al" in akas


def test_entity_stub_is_typed(graph: Graph, vocab: Vocabulary) -> None:
    assert (vocab.iri("Northgate"), RDF.type, vocab.classes["Location"]) in graph


def test_section_literal_present(graph: Graph, vocab: Vocabulary) -> None:
    personality = graph.value(vocab.iri("Alden"), vocab.section_property("personality"))
    assert personality is not None and "protective" in str(personality)
