"""Persistence for the RDF graph: load from / save to a Turtle file.

The canonical store for this POC is an in-memory :class:`rdflib.Graph` serialized
to a human-readable, git-friendly ``.ttl`` file. For larger graphs the same
rdflib API can be backed by an embedded Oxigraph triplestore -- see
:func:`oxigraph_graph` (requires the optional ``oxigraph`` extra).
"""

from __future__ import annotations

from pathlib import Path

from rdflib import Graph

from .ontology import Vocabulary


def new_graph(vocab: Vocabulary) -> Graph:
    """Return an empty graph with ``vocab``'s prefixes bound."""
    graph = Graph()
    for prefix, namespace in vocab.prefixes.items():
        graph.bind(prefix, namespace)
    return graph


def load_graph(path: str | Path, vocab: Vocabulary) -> Graph:
    """Load a Turtle file into a fresh, prefix-bound graph."""
    graph = new_graph(vocab)
    graph.parse(str(path), format="turtle")
    return graph


def save_graph(graph: Graph, path: str | Path) -> Path:
    """Serialize ``graph`` to ``path`` as Turtle, creating parent dirs."""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    graph.serialize(destination=str(out), format="turtle")
    return out


def oxigraph_graph(db_path: str | Path, vocab: Vocabulary) -> Graph:
    """Return an rdflib Graph backed by an on-disk Oxigraph store.

    Requires ``poetry install -E oxigraph``. Kept as the documented swap point
    for scaling beyond the in-memory Turtle store; unused by the default POC.
    """
    graph = Graph(store="Oxigraph")  # provided by the oxrdflib plugin
    graph.open(str(db_path), create=True)
    for prefix, namespace in vocab.prefixes.items():
        graph.bind(prefix, namespace)
    return graph
