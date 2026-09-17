"""Map authored YAML entity files onto RDF triples.

Two kinds of input live under ``data/``:

* ``entities.yaml`` -- a flat mapping of ``id: type`` for every non-character node
  (places, clans, bloodlines, jutsu, and referenced NPCs). These give the graph a
  typed node for each relation target, which is what lets SHACL enforce that (say)
  ``mentorOf`` points at a real ``Character``.
* ``characters/*.yaml`` / ``locations/*.yaml`` -- one record per character/place,
  carrying structured relation lists (become object-property edges) and a
  ``sections:`` map of prose (becomes literal properties, so the Markdown export
  can be full-fidelity).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from rdflib import RDF, Graph, Literal

from .ontology import Vocabulary, load_vocabulary
from .store import new_graph


def _load_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _add_entity(graph: Graph, record: dict[str, Any], vocab: Vocabulary) -> None:
    """Add a single character/entity record's triples to ``graph``."""
    if "id" not in record or "type" not in record:
        raise ValueError(f"Entity record missing 'id'/'type': {record!r}")

    subject = vocab.iri(record["id"])
    types = record["type"]
    # Each authored type is stamped together with its ancestors, so a subclass
    # (e.g. SecondaryCharacter) is usable anywhere its parent is expected.
    for type_name in [types] if isinstance(types, str) else types:
        for class_iri in vocab.class_closure(vocab.class_iri(type_name)):
            graph.add((subject, RDF.type, class_iri))

    # Scalar literal fields.
    for field, predicate in vocab.scalar_properties.items():
        value = record.get(field)
        if value is not None and value != "":
            graph.add((subject, predicate, Literal(value)))

    # Repeatable 'aka' nicknames.
    for nickname in record.get("aka", []) or []:
        graph.add((subject, vocab.aka, Literal(nickname)))

    # Object-property relations (lists of entity ids).
    for field, predicate in vocab.object_properties.items():
        for target in record.get(field, []) or []:
            graph.add((subject, predicate, vocab.iri(target)))

    # Prose sections -> one literal per section.
    for key, text in (record.get("sections") or {}).items():
        if text:
            graph.add((subject, vocab.section_property(key), Literal(text)))


def build_graph(lorebook_dir: str | Path, vocab: Vocabulary | None = None) -> Graph:
    """Build the canonical graph from a lorebook directory's YAML files.

    ``vocab`` defaults to the one loaded from ``lorebook_dir`` itself; pass an
    explicit one to reuse an already-loaded vocabulary.
    """
    root = Path(lorebook_dir)
    if vocab is None:
        vocab = load_vocabulary(root)
    data_path = root / "data"
    graph = new_graph(vocab)

    # 1. Typed stub nodes for every referenced non-character entity. Each value
    #    is either a bare type string, or a mapping with 'type' and optional 'name'.
    entities_file = data_path / "entities.yaml"
    if entities_file.exists():
        for entity_id, spec in (_load_yaml(entities_file) or {}).items():
            record = {"id": entity_id}
            record.update(spec if isinstance(spec, dict) else {"type": spec})
            _add_entity(graph, record, vocab)

    # 2. Full character/place records.
    for record_dir in ("characters", "locations"):
        for record_file in sorted((data_path / record_dir).glob("*.yaml")):
            _add_entity(graph, _load_yaml(record_file), vocab)

    return graph
