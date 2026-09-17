"""Helpers for running SPARQL over one lorebook or the union of several.

A single lorebook query binds that lorebook's own prefixes (:func:`Vocabulary.prefixes`).
A cross-lorebook (``--all``) query merges every lorebook's built graph and binds the
*union* of their prefixes -- ``core:``/``schema:``/``rdfs:`` are shared, while each
lorebook's ``onto``/``entity`` labels come through as configured. If two lorebooks
reuse the same label for different namespaces, that clash is reported so the author
can give them distinct prefixes.

:func:`load_union` does that merge and :func:`to_json` serialises a result, so the CLI
and the API run the same query path instead of each assembling one.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from rdflib import Graph
from rdflib.query import Result

from .layout import GRAPH_REL
from .ontology import Prefixes, Vocabulary, load_vocabulary


class NotBuilt(RuntimeError):
    """Raised when a lorebook's graph has not been built yet.

    Carries the lorebook's name so the caller can name the command to run.
    """

    def __init__(self, name: str, directory: Path) -> None:
        super().__init__(f"{name!r} is not built yet")
        self.name = name
        self.directory = directory


def merge_prefixes(vocabs: list[Vocabulary]) -> tuple[Prefixes, list[str]]:
    """Union the prefix maps of ``vocabs``; return ``(merged, conflicting_labels)``.

    On a clash (same label, different namespace) the first binding wins and the
    label is reported in ``conflicting_labels``.
    """
    merged: Prefixes = {}
    conflicts: list[str] = []
    for vocab in vocabs:
        for label, namespace in vocab.prefixes.items():
            if label in merged:
                if str(merged[label]) != str(namespace) and label not in conflicts:
                    conflicts.append(label)
            else:
                merged[label] = namespace
    return merged, conflicts


def prologue(prefixes: Prefixes) -> str:
    """Render a SPARQL PREFIX prologue from a prefix map."""
    return "".join(f"PREFIX {label}: <{ns}>\n" for label, ns in prefixes.items())


def graph_path(name: str, directory: Path) -> Path:
    """The built graph inside ``directory``; raises :class:`NotBuilt` if it is missing."""
    path = directory / GRAPH_REL
    if not path.exists():
        raise NotBuilt(name, directory)
    return path


def load_union(lorebooks: Mapping[str, Path]) -> tuple[Graph, Prefixes, list[str]]:
    """Merge the built graphs of ``lorebooks`` (name -> directory) into one graph.

    Returns the merged graph, the union of the prefix maps, and the labels that map
    to more than one namespace. Raises :class:`NotBuilt` for the first lorebook whose
    graph is missing -- a partial union would silently answer for fewer lorebooks than
    the caller asked about.
    """
    graph = Graph()
    vocabs = []
    for name, directory in lorebooks.items():
        path = graph_path(name, directory)
        vocabs.append(load_vocabulary(directory))
        graph.parse(path, format="turtle")
    prefixes, conflicts = merge_prefixes(vocabs)
    return graph, prefixes, conflicts


def to_json(result: Result) -> str:
    """Serialise a SELECT or ASK result as SPARQL 1.1 Query Results JSON.

    CONSTRUCT and DESCRIBE return a graph rather than bindings; serialise those as
    RDF (``result.graph.serialize(...)``) instead of calling this.
    """
    data = result.serialize(format="json")
    if data is None:
        raise ValueError(f"rdflib returned no JSON for a {result.type} result")
    if isinstance(data, bytes):
        return data.decode("utf-8")
    return data
