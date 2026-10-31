# -*- coding: utf-8 -*-
"""A provider that refuses the tags a file carries (`refused_tags` in `providers.yaml`).

The guarantee is that no request reaches the provider, so every refusal below asserts
what the mocked transport saw, not only the status code.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import (
    EMAIL,
    PASSWORD,
    answer_body,
    chapter_id,
    delta,
    llm_service,
    make_note,
    make_story,
    sse_body,
)

from inkspire_api import llm
from inkspire_api.llm import RefusedProvider, UnknownModel, load_providers
from inkspire_api.settings import get_settings

TAGGED = "===== ink:meta\ntags: [perso, priority]\n===== ink:body\nOnce upon a time.\n"
PLAIN = "===== ink:meta\ntags: [priority]\n===== ink:body\nOnce upon a time.\n"
#: Over the default budget, so a save is due a context summary.
LONG = ("Jane Doe is the narrator. " * 500) + "\n\nThe end approaches now."


def collect(chunks) -> list[str]:
    async def run() -> list[str]:
        return [chunk async for chunk in chunks]

    return asyncio.run(run())


# --- reading the file ------------------------------------------------------


def test_refused_tags_are_read_and_normalised(tmp_path: Path) -> None:
    path = tmp_path / "providers.yaml"
    path.write_text(
        "p:\n  url: https://provider.test\n  refused_tags: [' Perso', perso, to-revise]\n",
        encoding="utf-8",
    )
    assert load_providers(path)["p"].refused_tags == ["perso", "to-revise"]


def test_a_provider_with_no_refused_tags_refuses_none(tmp_path: Path) -> None:
    path = tmp_path / "providers.yaml"
    path.write_text("p:\n  url: https://provider.test\n", encoding="utf-8")
    assert load_providers(path)["p"].refused_tags == []


def test_a_bad_refused_tag_fails_the_load_naming_the_file_and_the_provider(
    tmp_path: Path,
) -> None:
    path = tmp_path / "providers.yaml"
    path.write_text(
        "p:\n  url: https://provider.test\n  refused_tags: ['two words']\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match=r"providers\.yaml: provider \"p\".*two words"):
        load_providers(path)


# --- the service -----------------------------------------------------------


def test_stream_refuses_before_any_request_is_made(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=sse_body(delta("x")))

    service = llm_service(tmp_path, handler, refused_tags=["perso"])
    with pytest.raises(RefusedProvider, match=r'"p" is not used for files tagged "perso"'):
        collect(service.stream("p/m", "text", tags=("priority", "perso")))
    assert seen == []


def test_complete_refuses_before_any_request_is_made(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=answer_body("x"))

    service = llm_service(tmp_path, handler, refused_tags=["perso"])
    with pytest.raises(RefusedProvider):
        asyncio.run(service.complete("p/m", "prompt", tags=("perso",)))
    assert seen == []


def test_a_file_without_a_refused_tag_is_answered(tmp_path: Path) -> None:
    handler = lambda request: httpx.Response(200, text=sse_body(delta("Hel"), delta("lo")))  # noqa: E731
    service = llm_service(tmp_path, handler, refused_tags=["perso"])
    assert collect(service.stream("p/m", "text", tags=("priority",))) == ["Hel", "lo"]
    assert collect(service.stream("p/m", "text", tags=())) == ["Hel", "lo"]


def test_a_provider_that_refuses_nothing_answers_a_tagged_file(tmp_path: Path) -> None:
    handler = lambda request: httpx.Response(200, text=sse_body(delta("x")))  # noqa: E731
    service = llm_service(tmp_path, handler)
    assert collect(service.stream("p/m", "text", tags=("perso",))) == ["x"]


def test_an_unknown_provider_is_still_unknown_for_a_tagged_file(tmp_path: Path) -> None:
    service = llm_service(tmp_path, lambda request: httpx.Response(500), refused_tags=["perso"])
    with pytest.raises(UnknownModel):
        collect(service.stream("absent/m", "text", tags=("perso",)))


# --- the routes ------------------------------------------------------------


@pytest.fixture
def refusing_client(app, settings, tmp_path: Path, user):
    """A logged-in client whose provider `p` refuses `perso`, with a small model set.
    `requests` holds every request the provider received."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if json.loads(request.content).get("stream"):
            return httpx.Response(200, text=sse_body(delta("Then it rained.")))
        return httpx.Response(200, text=answer_body("The Wax Still Held."))

    built = llm_service(tmp_path, handler, refused_tags=["perso"])
    app.dependency_overrides[llm.get_service] = lambda: built
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"llm_small_model": "p/model"}
    )

    with TestClient(app) as client:
        client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
        client.requests = requests  # type: ignore[attr-defined]
        yield client


def story_chapter(client: TestClient, data_root: Path, text: str) -> str:
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": text}
    )
    return chapter_id(client, "Example Story", "first-chapter")


def note_named(client: TestClient, files_root: Path, text: str) -> str:
    make_note(files_root, "scratch.ink", text)
    tree = client.get("/api/notes/tree").json()
    return next(entry["id"] for entry in tree["files"] if entry["name"] == "scratch")


def test_generating_for_a_refused_tag_is_a_403_and_sends_nothing(
    refusing_client, data_root: Path
) -> None:
    file_id = story_chapter(refusing_client, data_root, TAGGED)

    response = refusing_client.post(
        f"/api/stories/file/{file_id}/generate", json={"model": "p/m"}
    )

    assert response.status_code == 403
    assert response.json()["code"] == 403
    assert '"p" is not used for files tagged "perso"' in response.json()["message"]
    assert refusing_client.requests == []


def test_generating_for_a_note_with_a_refused_tag_is_a_403(
    refusing_client, files_root: Path
) -> None:
    file_id = note_named(refusing_client, files_root, TAGGED)

    response = refusing_client.post(f"/api/notes/file/{file_id}/generate", json={"model": "p/m"})

    assert response.status_code == 403
    assert refusing_client.requests == []


def test_generating_for_a_file_without_the_tag_streams(
    refusing_client, data_root: Path
) -> None:
    file_id = story_chapter(refusing_client, data_root, PLAIN)

    response = refusing_client.post(
        f"/api/stories/file/{file_id}/generate", json={"model": "p/m"}
    )

    assert response.status_code == 200
    assert "Then it rained." in response.text
    assert len(refusing_client.requests) == 1


def test_an_untagged_file_streams(refusing_client, data_root: Path) -> None:
    file_id = story_chapter(refusing_client, data_root, "Once upon a time.")
    response = refusing_client.post(
        f"/api/stories/file/{file_id}/generate", json={"model": "p/m"}
    )
    assert response.status_code == 200


def test_a_tag_added_later_refuses_the_next_generation(
    refusing_client, data_root: Path
) -> None:
    file_id = story_chapter(refusing_client, data_root, PLAIN)
    ok = refusing_client.post(f"/api/stories/file/{file_id}/generate", json={"model": "p/m"})
    assert ok.status_code == 200
    refusing_client.requests.clear()

    refusing_client.put(f"/api/stories/file/{file_id}", json={"tags": ["perso"]})
    refused = refusing_client.post(
        f"/api/stories/file/{file_id}/generate", json={"model": "p/m"}
    )

    assert refused.status_code == 403
    assert refusing_client.requests == []


def test_proposing_a_title_for_a_refused_tag_is_a_403_and_sends_nothing(
    refusing_client, data_root: Path
) -> None:
    file_id = story_chapter(refusing_client, data_root, TAGGED)

    response = refusing_client.post(f"/api/stories/file/{file_id}/title")

    assert response.status_code == 403
    assert refusing_client.requests == []


def test_proposing_a_title_for_another_tag_works(refusing_client, data_root: Path) -> None:
    file_id = story_chapter(refusing_client, data_root, PLAIN)
    response = refusing_client.post(f"/api/stories/file/{file_id}/title")
    assert response.status_code == 200
    assert len(refusing_client.requests) == 1


def test_proposing_a_completion_for_a_refused_tag_is_a_403_in_both_spaces(
    refusing_client, data_root: Path, files_root: Path
) -> None:
    chapter = story_chapter(refusing_client, data_root, TAGGED)
    note = note_named(refusing_client, files_root, TAGGED)

    in_story = refusing_client.post(f"/api/stories/file/{chapter}/complete", json={"model": "p/m"})
    in_notes = refusing_client.post(f"/api/notes/file/{note}/complete", json={"model": "p/m"})

    assert (in_story.status_code, in_notes.status_code) == (403, 403)
    assert refusing_client.requests == []


def test_proposing_a_completion_for_another_tag_works(refusing_client, data_root: Path) -> None:
    file_id = story_chapter(refusing_client, data_root, PLAIN)
    response = refusing_client.post(
        f"/api/stories/file/{file_id}/complete", json={"model": "p/m"}
    )
    assert response.status_code == 200
    assert len(refusing_client.requests) == 1


# --- the background summary ------------------------------------------------


@pytest.mark.parametrize("tagged", [True, False])
def test_a_save_calls_the_summary_model_only_when_its_provider_allows_the_file(
    refusing_client, data_root: Path, tagged: bool
) -> None:
    header = "===== ink:meta\ntags: [perso]\n===== ink:body\n" if tagged else ""
    file_id = story_chapter(refusing_client, data_root, header + "Once.\n")

    response = refusing_client.put(
        f"/api/stories/file/{file_id}/document", json={"body": LONG}
    )

    assert response.status_code == 200
    assert len(refusing_client.requests) == (0 if tagged else 1)


def test_a_refused_summary_does_not_spend_the_summary_budget(
    refusing_client, app, data_root: Path, monkeypatch
) -> None:
    file_id = story_chapter(
        refusing_client, data_root, "===== ink:meta\ntags: [perso]\n===== ink:body\nOnce.\n"
    )
    spent: list[str] = []
    monkeypatch.setattr(
        app.state.summary_limiter, "consume", lambda key: spent.append(key) or True
    )

    refusing_client.put(f"/api/stories/file/{file_id}/document", json={"body": LONG})

    assert spent == []
