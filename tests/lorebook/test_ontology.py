"""The Vocabulary loader: core + extension merge, range union, multi-tenancy."""

from __future__ import annotations

from pathlib import Path

import pytest

from lorebook import authoring
from lorebook.ontology import CORE, RDF, Vocabulary, load_vocabulary
from lorebook.validation import SH, build_shapes, validate


def _locals(uris) -> set[str]:
    return {str(u).rsplit("#", 1)[-1].rsplit("/", 1)[-1] for u in uris}


def test_core_and_extension_merge(vocab: Vocabulary) -> None:
    # Universal terms come from the shared core namespace...
    assert str(vocab.classes["Character"]).startswith(str(CORE))
    assert str(vocab.object_properties["mentorOf"]).startswith(str(CORE))
    # ...lorebook-specific terms mint in this lorebook's own onto namespace.
    assert str(vocab.classes["Guild"]).startswith(str(vocab.onto))
    assert str(vocab.object_properties["hasSkill"]).startswith(str(vocab.onto))


def test_extension_unions_range_onto_core_property(vocab: Vocabulary) -> None:
    # core memberOf ranges over Organization; the extension adds Guild.
    assert _locals(vocab.property_ranges["memberOf"]) == {"Organization", "Guild"}


def test_subclass_types_are_materialized(tmp_path: Path) -> None:
    """A subclass instance is stamped with its ancestors, so it IS its parent.

    Authoring `type: SecondaryCharacter` alone must be enough: the exporter, the
    generated shapes and plain `?c a core:Character` queries all match rdf:type
    exactly, so without the closure a secondary character would vanish from all
    three.
    """
    lb = _write_mini_lorebook(tmp_path / "mini")
    (lb / "data" / "characters" / "extra.yaml").write_text(
        "id: Innkeep\ntype: SecondaryCharacter\nname: Innkeep\n", encoding="utf-8"
    )
    vocab = load_vocabulary(lb)
    graph = authoring.build_graph(lb, vocab)

    innkeep = vocab.iri("Innkeep")
    assert (innkeep, RDF.type, vocab.classes["SecondaryCharacter"]) in graph
    assert (innkeep, RDF.type, CORE.Character) in graph  # the parent, materialized
    assert (innkeep, RDF.type, CORE.Entity) in graph     # ...and the root
    # The main cast keeps its plain type and does NOT become a secondary.
    assert (vocab.iri("Hero"), RDF.type, vocab.classes["SecondaryCharacter"]) not in graph

    conforms, report = validate(graph, vocab)
    assert conforms, report


def test_shapes_target_non_character_domains(vocab: Vocabulary) -> None:
    # `binds` has domain Skill, so a Skill NodeShape is generated too -- proving
    # shape generation is not hardcoded to Character.
    shapes = build_shapes(vocab)
    assert (None, SH.targetClass, vocab.classes["Skill"]) in shapes


def _write_mini_lorebook(root: Path) -> Path:
    (root / "data" / "characters").mkdir(parents=True)
    (root / "lorebook.yaml").write_text(
        "title: Mini\n"
        "namespaces:\n"
        "  onto: https://mini.example/onto#\n"
        "  entity: https://mini.example/entity#\n"
        "extension: extension.yaml\n",
        encoding="utf-8",
    )
    (root / "extension.yaml").write_text(
        "classes: [Guild]\nobject_properties:\n  memberOf: {range: Guild}\n",
        encoding="utf-8",
    )
    (root / "data" / "entities.yaml").write_text(
        "Ironforge: {type: [Location, Organization], name: Ironforge}\n"
        "MinersUnion: {type: Guild, name: Miners Union}\n",
        encoding="utf-8",
    )
    (root / "data" / "characters" / "hero.yaml").write_text(
        "id: Hero\ntype: Character\nname: Hero\nmemberOf: [MinersUnion, Ironforge]\n",
        encoding="utf-8",
    )
    return root


def _manifest(onto_prefix: str | None = None, entity_prefix: str | None = None) -> str:
    lines = [
        "title: Mini",
        "namespaces:",
        "  onto: https://mini.example/onto#",
        "  entity: https://mini.example/entity#",
    ]
    if onto_prefix or entity_prefix:
        lines.append("prefixes:")
        lines.append(f"  onto: {onto_prefix or 'lore'}")
        lines.append(f"  entity: {entity_prefix or 'ex'}")
    lines.append("extension: extension.yaml")
    return "\n".join(lines) + "\n"


def _write_manifest_only(root: Path, manifest: str) -> Path:
    root.mkdir(parents=True)
    (root / "lorebook.yaml").write_text(manifest, encoding="utf-8")
    (root / "extension.yaml").write_text("classes: []\n", encoding="utf-8")
    return root


def test_prefix_labels_are_configurable(tmp_path: Path) -> None:
    lb = _write_manifest_only(tmp_path / "mini", _manifest(onto_prefix="mini", entity_prefix="mx"))
    vocab = load_vocabulary(lb)
    assert vocab.onto_prefix == "mini" and vocab.entity_prefix == "mx"
    assert "mini" in vocab.prefixes and "lore" not in vocab.prefixes
    assert str(vocab.prefixes["mini"]) == "https://mini.example/onto#"


def test_reserved_prefix_label_is_rejected(tmp_path: Path) -> None:
    lb = _write_manifest_only(tmp_path / "bad", _manifest(onto_prefix="core"))
    with pytest.raises(ValueError, match="reserved"):
        load_vocabulary(lb)


def test_identical_onto_and_entity_prefixes_rejected(tmp_path: Path) -> None:
    lb = _write_manifest_only(tmp_path / "bad", _manifest(onto_prefix="x", entity_prefix="x"))
    with pytest.raises(ValueError, match="must differ"):
        load_vocabulary(lb)


def test_second_lorebook_is_independent(tmp_path: Path) -> None:
    lb = _write_mini_lorebook(tmp_path / "mini")
    vocab = load_vocabulary(lb)
    graph = authoring.build_graph(lb, vocab)

    # Its own class exists and mints in its own namespace...
    assert "Guild" in vocab.classes
    assert str(vocab.classes["Guild"]).startswith("https://mini.example/onto#")
    # ...and it shares the core vocabulary (Character/memberOf) with no extension terms.
    assert "Character" in vocab.classes
    assert "Bloodline" not in vocab.classes

    # Membership in a Guild (extension range) and an Organization (core range) both pass.
    conforms, report = validate(graph, vocab)
    assert conforms, report
