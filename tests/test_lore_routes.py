# -*- coding: utf-8 -*-
"""The lore routes: the graph, and one entity with its prose."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from tests.conftest import make_lorebook, make_story, story_id


@pytest.fixture
def story_with_lorebook(data_root: Path) -> Path:
    """A story carrying the fixture lorebook."""
    story = make_story(data_root, "example", title="Example Story")
    make_lorebook(story)
    return story


# --- what a story says it has -----------------------------------------------


def test_a_story_says_whether_it_has_a_lorebook(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}").json()
    assert body["lorebook"] is True
    assert body["timeline"] is False


def test_a_directory_without_a_manifest_is_not_a_lorebook(
    logged_in: TestClient, data_root: Path
) -> None:
    """A `lorebook/` with no `lorebook.yaml` in it is not one, the same way a directory
    with no `story.yaml` is not a story."""
    story = make_story(data_root, "example", title="Example Story")
    (story / "lorebook").mkdir()

    assert logged_in.get(f"/api/stories/dir/{story_id(logged_in)}").json()["lorebook"] is False


# --- the graph --------------------------------------------------------------


def test_the_graph_carries_nodes_and_links(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/lore/graph").json()
    assert sorted(node["label"] for node in body["nodes"]) == ["Example Guild", "Jane Doe"]
    assert [(link["label"]) for link in body["links"]] == ["memberOf"]


def test_a_node_carries_its_most_specific_class(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    """The legend colours and filters by `type`. The guild is an Organization as well as
    a Guild, and Guild is the deeper of the two, so that is what it comes back as."""
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/lore/graph").json()
    types = {node["label"]: node["type"] for node in body["nodes"]}
    assert types == {"Jane Doe": "Character", "Example Guild": "Guild"}

    guild = next(node for node in body["nodes"] if node["label"] == "Example Guild")
    assert "Organization" in guild["types"]


def test_a_node_carries_its_degree(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/lore/graph").json()
    assert all(node["degree"] == 1 for node in body["nodes"])


def test_the_graph_carries_no_prose(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    """Prose sections are dropped by design: too bulky for a tooltip, which is what the
    node attributes are for. This is why one entity can be asked for on its own."""
    body = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/lore/graph").json()
    for node in body["nodes"]:
        assert not any(key.startswith("section") for key in node["attrs"])
    assert "Steady." not in logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/graph"
    ).text


def test_reading_the_graph_writes_nothing(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    """The graph is built in memory, so no `build/` appears in the data repository."""
    lorebook = story_with_lorebook / "lorebook"
    before = sorted(path.name for path in lorebook.iterdir())

    logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/lore/graph")

    assert sorted(path.name for path in lorebook.iterdir()) == before
    assert not (lorebook / "build").exists()
    assert not (lorebook / "export").exists()


def test_the_graph_is_built_without_the_cli_having_run(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    """`build/lorebook.ttl` is what `lorebook build` writes, and is not what is read
    here — the authored YAML is, so the route works on a fresh clone."""
    assert not (story_with_lorebook / "lorebook" / "build").exists()
    assert logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/graph"
    ).status_code == 200


# --- one entity -------------------------------------------------------------


def test_an_entity_carries_its_prose(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    body = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/entity/Doe"
    ).json()
    assert body["sections"] == {"personality": "Steady.", "backstory": "From elsewhere."}
    assert body["local"] == "Doe"
    assert body["name"] == "Jane Doe"


def test_an_entitys_relations_come_back_as_labels(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    """`node_context` resolves an edge to the label at the other end, not to an id, so
    a relation cannot be followed back into the graph from here."""
    body = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/entity/Doe"
    ).json()
    assert body["memberOf"] == ["Example Guild"]


def test_an_entity_carries_its_types(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    body = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/entity/Doe"
    ).json()
    assert "Character" in body["types"]
    assert body["id"].endswith("#Doe")


def test_a_subclass_gets_the_layout_of_its_parent(
    logged_in: TestClient, data_root: Path
) -> None:
    """Subclass types are materialised, so a SecondaryCharacter carries Character and
    matches the character layout with no inference anywhere."""
    story = make_story(data_root, "example", title="Example Story")
    make_lorebook(
        story,
        characters={
            "Smith": {
                "id": "Smith",
                "type": "SecondaryCharacter",
                "name": "John Smith",
                "sections": {"personality": "Brisk."},
            }
        },
        entities={},
    )
    body = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/entity/Smith"
    ).json()
    assert body["sections"] == {"personality": "Brisk."}
    assert "SecondaryCharacter" in body["types"]


def test_an_entity_of_neither_kind_has_no_sections(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    """A stub is neither a character nor a place, so it has no section layout — which is
    exactly the node `export_all` writes no sheet for."""
    body = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/entity/ExampleGuild"
    ).json()
    assert body["sections"] == {}
    assert body["name"] == "Example Guild"


def test_the_response_does_not_carry_the_section_layout(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    """How a sheet is laid out is the template's business, not the response's."""
    body = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/entity/Doe"
    ).json()
    assert "section_order" not in body


# --- what answers what ------------------------------------------------------


def test_an_unknown_story_is_not_found(logged_in: TestClient, data_root: Path) -> None:
    assert (
        logged_in.get("/api/stories/dir/0123456789abcdef/lore/graph").status_code == 404
    )


def test_a_story_with_no_lorebook_is_not_found(
    logged_in: TestClient, data_root: Path
) -> None:
    make_story(data_root, "bare", title="Bare Story")
    response = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in, 'Bare Story')}/lore/graph"
    )
    assert response.status_code == 404
    assert "lorebook" in response.json()["message"]


def test_an_unknown_entity_is_not_found(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    response = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/entity/Nobody"
    )
    assert response.status_code == 404
    assert "Nobody" in response.json()["message"]


def test_an_undefined_type_is_the_writers_to_fix(
    logged_in: TestClient, data_root: Path
) -> None:
    """`Vocabulary.class_iri` refuses a type the vocabulary does not declare. The files
    are there and a person has to fix them, so it is a 422."""
    story = make_story(data_root, "example", title="Example Story")
    make_lorebook(
        story,
        characters={
            "Doe": {"id": "Doe", "type": "Dragon", "name": "Jane Doe", "sections": {}}
        },
        entities={},
    )
    response = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/lore/graph")
    assert response.status_code == 422
    assert "Dragon" in response.json()["message"]


def test_an_unreadable_manifest_is_unprocessable(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    (story_with_lorebook / "lorebook" / "lorebook.yaml").write_text(
        "title: Example\nnamespaces: not-a-mapping\n", encoding="utf-8"
    )
    response = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/lore/graph")
    assert response.status_code == 422


@pytest.mark.parametrize(
    "path", ["lore/graph", "lore/entity/Doe"]
)
def test_the_routes_refuse_a_request_with_no_token(
    client: TestClient, story_with_lorebook: Path, path: str
) -> None:
    assert client.get(f"/api/stories/dir/anything/{path}").status_code == 401


# --- the held build ---------------------------------------------------------


def test_an_edited_lorebook_is_built_again(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    """The build is held against the mtimes of every file it reads, so authoring a new
    character outside the app shows up."""
    first = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/lore/graph").json()
    assert len(first["nodes"]) == 2

    (story_with_lorebook / "lorebook" / "data" / "characters" / "smith.yaml").write_text(
        yaml.safe_dump(
            {
                "id": "Smith",
                "type": "Character",
                "name": "John Smith",
                "memberOf": ["ExampleGuild"],
                "sections": {"personality": "Brisk."},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    second = logged_in.get(f"/api/stories/dir/{story_id(logged_in)}/lore/graph").json()
    assert len(second["nodes"]) == 3


def test_an_edited_extension_is_read_again(
    logged_in: TestClient, story_with_lorebook: Path
) -> None:
    """The vocabulary is part of what a build reads, so the stamp covers it too."""
    assert logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/graph"
    ).status_code == 200

    (story_with_lorebook / "lorebook" / "extension.yaml").write_text(
        "classes: [Guild]\nobject_properties: {memberOf: {range: Nonexistent}}\n",
        encoding="utf-8",
    )

    assert logged_in.get(
        f"/api/stories/dir/{story_id(logged_in)}/lore/graph"
    ).status_code == 422


def test_two_stories_hold_their_own_graphs(
    logged_in: TestClient, data_root: Path
) -> None:
    """One cache holds a build per story, so one story's lorebook cannot answer for
    another's."""
    first = make_story(data_root, "first", title="First Story")
    make_lorebook(first, title="First Lorebook")
    second = make_story(data_root, "second", title="Second Story")
    make_lorebook(
        second,
        title="Second Lorebook",
        characters={
            "Smith": {
                "id": "Smith",
                "type": "Character",
                "name": "John Smith",
                "sections": {"personality": "Brisk."},
            }
        },
        entities={},
    )

    one = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in, 'First Story')}/lore/graph"
    ).json()
    two = logged_in.get(
        f"/api/stories/dir/{story_id(logged_in, 'Second Story')}/lore/graph"
    ).json()

    assert sorted(node["label"] for node in one["nodes"]) == [
        "Example Guild",
        "Jane Doe",
    ]
    assert sorted(node["label"] for node in two["nodes"]) == ["John Smith"]
