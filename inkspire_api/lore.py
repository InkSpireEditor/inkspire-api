# -*- coding: utf-8 -*-
"""A story's lorebook, served as graph data.

    GET /api/stories/dir/{id}/lore/graph
    GET /api/stories/dir/{id}/lore/entity/{local}

The `lorebook` package does the work. The graph is built in memory from the authored
YAML on each cache miss — `build/lorebook.ttl` is what the CLI writes and is not read
here, and nothing under the story directory is written, so opening a view adds no file
to the data repository. What the browser draws therefore follows the YAML, which is
canonical, rather than whatever `lorebook build` last wrote.

The graph payload carries no prose: `build_view_data` drops every `section*` literal,
which is right for a tooltip and is why one entity can be asked for on its own.

SHACL does not run here. Generating the shapes and validating would pay an
authoring-time check on every read, and a graph that does not conform still draws;
`lorebook validate` is where that belongs.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from rdflib import Graph, URIRef

from lorebook import authoring, export, view
from lorebook.ontology import RDF, Vocabulary, load_vocabulary

from .files import ScannerDep
from .fs import HeldPerKey, Malformed, NotFound, mtime
from .storage import LOREBOOK, LOREBOOK_MANIFEST, Scanner

router = APIRouter(prefix="/stories/dir", tags=["lore"])

#: Files a build reads, so a change to any of them is a change to the graph. The
#: manifest and the extension declare the vocabulary; everything under `data/` is the
#: content.
VOCABULARY_FILES = (LOREBOOK_MANIFEST, "extension.yaml")
DATA_DIR = "data"


@dataclass(frozen=True)
class Built:
    """One story's lorebook, as the routes need it."""

    graph: Graph
    vocab: Vocabulary


def lorebook_dir(scanner: Scanner, story_id: str) -> Path:
    """Where the story's lorebook is, whether or not anything is there."""
    story = scanner.story(story_id)
    return scanner.path(story.relpath / LOREBOOK)


def build(scanner: Scanner, story_id: str) -> Built:
    """The story's graph, built from its YAML.

    Raises `NotFound` when the story has no lorebook, and `Malformed` when it has one
    the vocabulary or the authored files cannot be read against.
    """
    directory = lorebook_dir(scanner, story_id)
    if not (directory / LOREBOOK_MANIFEST).is_file():
        raise NotFound(
            f"This story has no lorebook. Write a {LOREBOOK}/{LOREBOOK_MANIFEST} "
            "to give it one."
        )
    try:
        vocab = load_vocabulary(directory)
        return Built(graph=authoring.build_graph(directory, vocab), vocab=vocab)
    except (ValueError, KeyError, TypeError) as exc:
        # An entity typed with a class the vocabulary does not define, a manifest
        # missing what it has to declare, or YAML that is not the shape expected.
        raise Malformed(f"This story's lorebook cannot be read: {exc}") from exc


def source_stamp(scanner: Scanner, story_id: str) -> tuple:
    """The mtimes of every file a build reads, so an edit to any of them rebuilds."""
    directory = lorebook_dir(scanner, story_id)
    named = tuple(mtime(directory / name) for name in VOCABULARY_FILES)
    authored = tuple(
        (str(path.relative_to(directory)), mtime(path))
        for path in sorted((directory / DATA_DIR).rglob("*.yaml"))
    )
    return (named, authored)


def entity_context(built: Built, local: str) -> dict:
    """One entity as the Markdown sheet sees it, prose included.

    The section layout comes from what the subject is: `export` keys one to the
    character class and one to the location class, and subclass types are materialised,
    so a `SecondaryCharacter` carries `core:Character` and matches without inference.
    A subject that is neither answers with its fields and no sections, which is exactly
    the node the exporter skips.
    """
    subject = built.vocab.iri(local)
    types = set(built.graph.objects(subject, RDF.type))
    if not types:
        raise NotFound(f'Nothing in this lorebook is called "{local}".')

    section_order = export.section_order_for(built.graph, subject, built.vocab)
    context = export.node_context(built.graph, subject, built.vocab, section_order)
    # `section_order` is how the template lays a sheet out, which is the caller's
    # business rather than the response's.
    context.pop("section_order", None)
    context["id"] = str(subject)
    context["local"] = local
    context["types"] = sorted(
        str(one).rsplit("#", 1)[-1].rsplit("/", 1)[-1]
        for one in types
        if isinstance(one, URIRef)
    )
    return context


def get_lorebooks(request: Request, scanner: ScannerDep) -> HeldPerKey[Built]:
    """One cache per application, holding each story's built graph."""
    if request.app.state.lorebooks is None:
        request.app.state.lorebooks = HeldPerKey(
            lambda story_id: build(scanner, story_id),
            lambda story_id: source_stamp(scanner, story_id),
        )
    return request.app.state.lorebooks


LorebooksDep = Annotated[HeldPerKey[Built], Depends(get_lorebooks)]


@router.get("/{dir_id}/lore/graph")
def lore_graph(dir_id: str, lorebooks: LorebooksDep) -> dict:
    """The lorebook as nodes and links, without the prose.

    A node carries its most specific class as `type`, which is what the legend colours
    and filters by, and `degree`, which is how many edges reach it.
    """
    built = lorebooks.get(dir_id)
    return view.build_view_data(built.graph, built.vocab)


@router.get("/{dir_id}/lore/entity/{local}")
def lore_entity(dir_id: str, local: str, lorebooks: LorebooksDep) -> dict:
    """One entity: its fields, its relations as labels, and its prose sections."""
    return entity_context(lorebooks.get(dir_id), local)
