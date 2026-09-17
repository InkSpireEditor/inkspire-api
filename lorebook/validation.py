"""Validate the graph against SHACL shapes generated from the vocabulary.

The referential-integrity rules -- every character named, every relation pointing
at a real node of the right type -- are no longer hand-written. They are *derived*
from the vocabulary's ``rdfs:domain``/``rdfs:range`` declarations, so authoring and
validation can never drift apart: declaring a property's range in ``core.ttl`` or an
``extension.yaml`` is what makes ``lorebook validate`` enforce it.
"""

from __future__ import annotations

from pyshacl import validate as _pyshacl_validate
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection
from rdflib.namespace import RDF
from rdflib.term import Node

from .ontology import SDO, Vocabulary

SH = Namespace("http://www.w3.org/ns/shacl#")


def build_shapes(vocab: Vocabulary) -> Graph:
    """Generate a SHACL shapes graph from ``vocab``'s domains and ranges."""
    shapes = Graph()
    shapes.bind("sh", SH)
    node_shapes: dict[URIRef, URIRef] = {}

    def node_shape(cls: URIRef) -> URIRef:
        """Return (creating on first use) the NodeShape targeting ``cls``."""
        if cls not in node_shapes:
            shape = URIRef(str(cls) + "Shape")
            shapes.add((shape, RDF.type, SH.NodeShape))
            shapes.add((shape, SH.targetClass, cls))
            node_shapes[cls] = shape
        return node_shapes[cls]

    # Every Character/Location must be named.
    for named_class in (vocab.character_class, vocab.location_class):
        name_prop = BNode()
        shapes.add((node_shape(named_class), SH.property, name_prop))
        shapes.add((name_prop, SH.path, SDO.name))
        shapes.add((name_prop, SH.minCount, Literal(1)))
        shapes.add((name_prop, SH.message, Literal(f"Every {_local(named_class)} must have a schema:name.")))

    # Each object property: its object must be an instance of the declared range,
    # attached to the NodeShape of each declared domain (default: Character).
    for field, prop in vocab.object_properties.items():
        ranges = vocab.property_ranges.get(field, [])
        if not ranges:
            continue
        for domain in vocab.domains(prop) or [vocab.character_class]:
            pshape = BNode()
            shapes.add((node_shape(domain), SH.property, pshape))
            shapes.add((pshape, SH.path, prop))
            if len(ranges) == 1:
                shapes.add((pshape, SH["class"], ranges[0]))
                names = _local(ranges[0])
            else:
                # Typed as the list Collection wants, which is invariant: a
                # `list[BNode]` is not a `list[Node]`.
                alternatives: list[Node] = []
                for rng in ranges:
                    alt = BNode()
                    shapes.add((alt, SH["class"], rng))
                    alternatives.append(alt)
                or_list = BNode()
                Collection(shapes, or_list, alternatives)
                shapes.add((pshape, SH["or"], or_list))
                names = " or ".join(_local(r) for r in ranges)
            shapes.add((pshape, SH.message, Literal(f"{field} must point at a {names}.")))

    return shapes


def _local(term: URIRef) -> str:
    return str(term).rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def validate(graph: Graph, vocab: Vocabulary) -> tuple[bool, str]:
    """Validate ``graph`` against ``vocab``'s generated shapes.

    Returns ``(conforms, human_readable_report)``.
    """
    conforms, _report_graph, report_text = _pyshacl_validate(
        graph,
        shacl_graph=build_shapes(vocab),
        inference="none",
        advanced=True,
        meta_shacl=False,
    )
    return conforms, report_text
