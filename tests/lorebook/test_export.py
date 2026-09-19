"""Markdown export renders graph facts into the character sheet."""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph

from lorebook import export as export_mod
from lorebook.ontology import Vocabulary


def test_context_resolves_related_labels(graph: Graph, vocab: Vocabulary) -> None:
    context = export_mod.character_context(graph, vocab.iri("Alden"), vocab)
    assert context["name"] == "Alden"
    assert context["rank"] == "Master"
    # Related entities resolve to their schema:name labels, sorted.
    assert context["mentorOf"] == ["Bryn", "Cora"]
    assert context["wields"] == ["Emberstone"]
    assert "personality" in context["sections"]


def test_export_writes_markdown(graph: Graph, vocab: Vocabulary, tmp_path: Path) -> None:
    written = export_mod.export_all(graph, tmp_path, vocab)
    # Only characters carrying prose sections get a sheet.
    names = {p.name for p in written}
    assert names == {"Alden.md", "Bryn.md", "Cora.md"}

    alden = (tmp_path / "Alden.md").read_text(encoding="utf-8")
    assert "# Alden" in alden
    assert "Master" in alden
    assert "Bryn" in alden and "Cora" in alden
    assert "## Personality and Traits" in alden
