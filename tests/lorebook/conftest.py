"""Shared fixtures: a self-contained TEST lorebook, its vocabulary, and its graph.

Tests deliberately run against ``tests/fixtures/testbook`` -- a small, stable,
made-up lorebook -- not against any real lorebook under ``lorebooks/``. That keeps
the suite independent of authored content: editing (or deleting) a real lorebook
never breaks the tests.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from rdflib import Graph

from lorebook import authoring
from lorebook.ontology import Vocabulary, load_vocabulary

LOREBOOK_DIR = Path(__file__).resolve().parent / "fixtures" / "testbook"


@pytest.fixture(scope="session")
def lorebook_dir() -> Path:
    return LOREBOOK_DIR


@pytest.fixture()
def vocab(lorebook_dir: Path) -> Vocabulary:
    return load_vocabulary(lorebook_dir)


@pytest.fixture()
def graph(lorebook_dir: Path, vocab: Vocabulary) -> Graph:
    return authoring.build_graph(lorebook_dir, vocab)


@pytest.fixture()
def make_lorebook() -> Callable[..., Path]:
    """Factory writing a minimal, buildable lorebook at a given directory.

    One character and nothing else -- enough to build, validate and query, so tests
    about layout and the CLI do not depend on the shape of the testbook fixture.
    """

    def _make(
        directory: Path,
        name: str,
        *,
        onto_prefix: str = "lore",
        entity_prefix: str = "ex",
    ) -> Path:
        (directory / "data" / "characters").mkdir(parents=True)
        (directory / "lorebook.yaml").write_text(
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
        (directory / "extension.yaml").write_text("classes: []\n", encoding="utf-8")
        (directory / "data" / "characters" / "hero.yaml").write_text(
            f"id: Hero_{name}\ntype: Character\nname: Hero {name}\n", encoding="utf-8"
        )
        return directory

    return _make
