"""SPARQL over the graph answers relational questions.

Prefix labels are configurable per lorebook (``vocab.onto_prefix`` /
``vocab.entity_prefix``), so these queries are built from those labels rather than
hardcoding any -- only ``core:``/``schema:`` are fixed. The test fixture uses
non-default labels (tb/tbx) precisely to catch accidental hardcoding.
"""

from __future__ import annotations

from rdflib import Graph

from lorebook.ontology import Vocabulary


def _prologue(vocab: Vocabulary) -> str:
    return "".join(f"PREFIX {p}: <{ns}>\n" for p, ns in vocab.prefixes.items())


def test_who_does_alden_mentor(graph: Graph, vocab: Vocabulary) -> None:
    ex = vocab.entity_prefix
    rows = graph.query(_prologue(vocab) + f"SELECT ?s WHERE {{ {ex}:Alden core:mentorOf ?s }}")
    mentees = {str(row[0]).rsplit("#", 1)[-1] for row in rows}
    assert mentees == {"Bryn", "Cora"}


def test_characters_with_firecraft(graph: Graph, vocab: Vocabulary) -> None:
    onto, ex = vocab.onto_prefix, vocab.entity_prefix
    rows = graph.query(
        _prologue(vocab)
        + f"""
        SELECT DISTINCT ?c WHERE {{
            ?c {onto}:hasSkill {ex}:Firecraft .
        }}
        """
    )
    names = {str(row[0]).rsplit("#", 1)[-1] for row in rows}
    assert names == {"Alden", "Bryn"}
