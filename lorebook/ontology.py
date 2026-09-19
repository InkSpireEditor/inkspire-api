"""The lorebook vocabulary, assembled at runtime for one lorebook.

Historically this module hardcoded the vocabulary as Python dicts. It now loads a
:class:`Vocabulary` from declarative, human-readable sources so lorebooks can add
their own classes/fields without touching code:

* the shared **core ontology** ``lorebook/core.ttl`` (Turtle: universal terms), plus
* a per-lorebook **extension** (``extension.yaml``) that declares only its additions,
  minting terms in that lorebook's own ``onto`` namespace.

The loader reads both, following the conventions documented in ``core.ttl`` (class =
``owl:Class``, object property = ``owl:ObjectProperty``, scalar = ``owl:DatatypeProperty``,
field name = the term's local name, ``rdfs:domain``/``rdfs:range`` drive validation).
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, SDO, DefinedNamespaceMeta
from rdflib.term import Node

#: Namespace of the shared core vocabulary (see ``core.ttl``).
CORE = Namespace("https://lorebook.example/core#")

#: A prefix label mapped to the namespace it stands for. ``schema:`` and ``rdfs:`` are
#: rdflib ``DefinedNamespace`` classes, not ``Namespace`` instances, so both belong in
#: the value type; binding a prefix and writing a prologue accept either.
Prefixes = dict[str, Namespace | DefinedNamespaceMeta]

__all__ = ["Vocabulary", "load_vocabulary", "CORE", "Prefixes", "RDF", "RDFS", "SDO"]


def _local(term: Node) -> str:
    """Local name of a URI: the fragment, else the last path segment."""
    return str(term).rsplit("#", 1)[-1].rsplit("/", 1)[-1]


@dataclass
class Vocabulary:
    """Everything the engine needs to map authored fields onto RDF for one lorebook."""

    title: str
    onto: Namespace                          #: this lorebook's term namespace (``lore:`` by default)
    entity: Namespace                        #: this lorebook's instance namespace (``ex:`` by default)
    onto_prefix: str                         #: prefix label bound to ``onto`` (configurable)
    entity_prefix: str                       #: prefix label bound to ``entity`` (configurable)
    ontology_graph: Graph                    #: core + extension terms as RDF
    classes: dict[str, URIRef]               #: class name -> class IRI
    object_properties: dict[str, URIRef]     #: field -> predicate IRI (node-valued)
    scalar_properties: dict[str, URIRef]     #: field -> predicate IRI (literal-valued)
    property_ranges: dict[str, list[URIRef]]  #: object-property field -> range class IRIs
    aka: URIRef                              #: repeatable-nickname predicate
    character_class: URIRef                  #: the ``Character`` class IRI
    location_class: URIRef                   #: the ``Location`` class IRI

    def iri(self, local_id: str) -> URIRef:
        """Entity IRI for a local id (e.g. ``"Doe"`` -> ``ex:Doe``)."""
        return self.entity[local_id]

    def class_iri(self, type_name: str) -> URIRef:
        """Resolve an authored ``type:`` string to its class IRI."""
        try:
            return self.classes[type_name]
        except KeyError as exc:
            raise ValueError(
                f"Unknown entity type {type_name!r}; expected one of {sorted(self.classes)}"
            ) from exc

    def section_property(self, key: str) -> URIRef:
        """Literal predicate for a prose-section key (e.g. ``"personality"``)."""
        return self.onto[f"section{key[:1].upper()}{key[1:]}"]

    def domains(self, prop: URIRef) -> list[URIRef]:
        """Declared ``rdfs:domain`` classes of a property (may be empty)."""
        return [URIRef(str(d)) for d in self.ontology_graph.objects(prop, RDFS.domain)]

    def class_closure(self, cls: URIRef) -> list[URIRef]:
        """``cls`` followed by every ancestor reachable via ``rdfs:subClassOf``.

        This is what makes subclassing real rather than decorative: the authoring
        layer stamps the whole closure as ``rdf:type``, so a
        ``core:SecondaryCharacter`` is also a ``core:Character`` for the exporter,
        the generated SHACL shapes, and plain ``?c a core:Character`` queries --
        none of which do subclass reasoning of their own.
        """
        closure: list[URIRef] = []
        for ancestor in self.ontology_graph.transitive_objects(cls, RDFS.subClassOf):
            if isinstance(ancestor, URIRef) and ancestor not in closure:
                closure.append(ancestor)
        return closure

    @property
    def prefixes(self) -> Prefixes:
        """Prefix bindings for readable Turtle / SPARQL on this lorebook's graphs.

        ``core:``/``schema:``/``rdfs:`` are shared across all lorebooks; the labels
        for this lorebook's own ``onto``/``entity`` namespaces are configurable via
        the manifest (defaulting to ``lore``/``ex``), which lets a cross-lorebook
        query address each lorebook's terms unambiguously.

        ``schema:`` and ``rdfs:`` are rdflib's ``DefinedNamespace`` classes rather
        than ``Namespace`` instances, so the value type covers both. Both are
        accepted wherever a prefix is bound or a prologue is written.
        """
        return {
            "core": CORE,
            self.onto_prefix: self.onto,
            self.entity_prefix: self.entity,
            "schema": SDO,
            "rdfs": RDFS,
        }


def _core_ontology() -> Graph:
    graph = Graph()
    graph.parse(data=resources.files("lorebook").joinpath("core.ttl").read_text("utf-8"), format="turtle")
    return graph


def load_vocabulary(lorebook_dir: str | Path) -> Vocabulary:
    """Assemble the :class:`Vocabulary` for the lorebook rooted at ``lorebook_dir``."""
    root = Path(lorebook_dir)
    manifest: dict[str, Any] = yaml.safe_load((root / "lorebook.yaml").read_text("utf-8")) or {}
    ns = manifest.get("namespaces", {})
    onto = Namespace(ns["onto"])
    entity = Namespace(ns["entity"])

    # Prefix labels are configurable (default lore/ex); they must not shadow the
    # shared reserved prefixes, and the two must differ.
    prefix_cfg = manifest.get("prefixes") or {}
    onto_prefix = prefix_cfg.get("onto", "lore")
    entity_prefix = prefix_cfg.get("entity", "ex")
    reserved = {"core", "schema", "rdfs"}
    for label in (onto_prefix, entity_prefix):
        if label in reserved:
            raise ValueError(f"Prefix label {label!r} in {root.name!r} is reserved ({sorted(reserved)}).")
    if onto_prefix == entity_prefix:
        raise ValueError(f"onto and entity prefixes in {root.name!r} must differ (both {onto_prefix!r}).")

    graph = _core_ontology()
    classes: dict[str, URIRef] = {}
    object_properties: dict[str, URIRef] = {}
    scalar_properties: dict[str, URIRef] = {}
    property_ranges: dict[str, list[URIRef]] = {}

    def add_ranges(field: str, ranges: list[URIRef]) -> None:
        bucket = property_ranges.setdefault(field, [])
        bucket.extend(r for r in ranges if r not in bucket)

    # --- terms declared by the core ontology ---
    for cls in graph.subjects(RDF.type, OWL.Class):
        classes[_local(cls)] = URIRef(str(cls))
    for prop in graph.subjects(RDF.type, OWL.ObjectProperty):
        object_properties[_local(prop)] = URIRef(str(prop))
        add_ranges(_local(prop), [URIRef(str(r)) for r in graph.objects(prop, RDFS.range)])
    for prop in graph.subjects(RDF.type, OWL.DatatypeProperty):
        scalar_properties[_local(prop)] = URIRef(str(prop))

    # --- fold in the lorebook's extension ---
    ext_ref = manifest.get("extension")
    ext: dict[str, Any] = (yaml.safe_load((root / ext_ref).read_text("utf-8")) or {}) if ext_ref else {}

    for name in ext.get("classes") or []:  # classes first, so ranges can resolve them
        uri = onto[name]
        classes[name] = uri
        graph.add((uri, RDF.type, OWL.Class))
        graph.add((uri, RDFS.subClassOf, CORE.Entity))
        graph.add((uri, RDFS.label, Literal(name)))

    def resolve_class(name: str) -> URIRef:
        try:
            return classes[name]
        except KeyError as exc:
            raise ValueError(f"Unknown class {name!r} referenced in extension of {root.name!r}") from exc

    for field, spec in (ext.get("object_properties") or {}).items():
        spec = spec or {}
        raw_range = spec.get("range", [])
        ranges = [resolve_class(r) for r in ([raw_range] if isinstance(raw_range, str) else raw_range)]
        if field in object_properties:
            prop = object_properties[field]  # extend an existing (core) property: union ranges
        else:
            prop = onto[field]
            object_properties[field] = prop
            graph.add((prop, RDF.type, OWL.ObjectProperty))
            if spec.get("domain"):
                graph.add((prop, RDFS.domain, resolve_class(spec["domain"])))
        for rng in ranges:
            graph.add((prop, RDFS.range, rng))
        add_ranges(field, ranges)

    for field in (ext.get("scalar_properties") or {}):
        if field not in scalar_properties:
            prop = onto[field]
            scalar_properties[field] = prop
            graph.add((prop, RDF.type, OWL.DatatypeProperty))

    aka = scalar_properties.pop("aka", CORE.aka)  # nicknames are authored/handled specially

    return Vocabulary(
        title=manifest.get("title", "Lorebook"),
        onto=onto,
        entity=entity,
        onto_prefix=onto_prefix,
        entity_prefix=entity_prefix,
        ontology_graph=graph,
        classes=classes,
        object_properties=object_properties,
        scalar_properties=scalar_properties,
        property_ranges=property_ranges,
        aka=aka,
        character_class=classes["Character"],
        location_class=classes["Location"],
    )
