"""Cross-lorebook querying: prefix union, conflict detection, merged-graph SPARQL."""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph

from lorebook import authoring
from lorebook.ontology import load_vocabulary
from lorebook.querying import merge_prefixes, prologue


def _mini(root: Path, name: str, *, onto_prefix: str = "lore", entity_prefix: str = "ex") -> Path:
    (root / "data" / "characters").mkdir(parents=True)
    (root / "lorebook.yaml").write_text(
        f"title: {name}\n"
        "namespaces:\n"
        f"  onto: https://{name}.example/onto#\n"
        f"  entity: https://{name}.example/entity#\n"
        "prefixes:\n"
        f"  onto: {onto_prefix}\n"
        f"  entity: {entity_prefix}\n"
        "extension: extension.yaml\n",
        encoding="utf-8",
    )
    (root / "extension.yaml").write_text("classes: []\n", encoding="utf-8")
    (root / "data" / "characters" / "hero.yaml").write_text(
        f"id: Hero_{name}\ntype: Character\nname: Hero {name}\n", encoding="utf-8"
    )
    return root


def test_shared_labels_conflict_but_core_does_not(tmp_path: Path) -> None:
    # Two lorebooks both defaulting to lore:/ex: -> those labels clash.
    a = load_vocabulary(_mini(tmp_path / "a", "a"))
    b = load_vocabulary(_mini(tmp_path / "b", "b"))
    _merged, conflicts = merge_prefixes([a, b])
    assert set(conflicts) == {"lore", "ex"}
    assert "core" not in conflicts and "schema" not in conflicts  # shared, identical


def test_distinct_labels_have_no_conflict(tmp_path: Path) -> None:
    a = load_vocabulary(_mini(tmp_path / "a", "a", onto_prefix="alpha", entity_prefix="ax"))
    b = load_vocabulary(_mini(tmp_path / "b", "b", onto_prefix="beta", entity_prefix="bx"))
    merged, conflicts = merge_prefixes([a, b])
    assert not conflicts
    assert {"alpha", "beta", "ax", "bx", "core"} <= set(merged)


def test_query_spans_all_lorebooks_via_core_vocabulary(tmp_path: Path) -> None:
    a = _mini(tmp_path / "a", "a", onto_prefix="alpha", entity_prefix="ax")
    b = _mini(tmp_path / "b", "b", onto_prefix="beta", entity_prefix="bx")
    va, vb = load_vocabulary(a), load_vocabulary(b)

    merged = Graph()
    for src in (authoring.build_graph(a, va), authoring.build_graph(b, vb)):
        for triple in src:
            merged.add(triple)

    prefixes, _ = merge_prefixes([va, vb])
    rows = merged.query(
        prologue(prefixes) + "SELECT ?n WHERE { ?c a core:Character ; schema:name ?n }"
    )
    assert {str(r[0]) for r in rows} == {"Hero a", "Hero b"}
