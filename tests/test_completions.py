# -*- coding: utf-8 -*-
"""`clean_completion`, `assemble_completion`, and the two
`POST .../file/{id}/complete` routes (frontend#9).

The route's own prompt rendering is pinned in `tests/data/prompts.json` through
`test_llm.py`'s parametrised test, the same way the continuation/fim/rewrite/title
prompts are -- this file is about the route's behaviour, not the exact wording it
sends.
"""

from __future__ import annotations

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
    llm_service,
    make_note,
    make_story,
)
from tests.test_llm import COMPLETION_PROMPTS

from inkspire_api import completions, llm
from inkspire_api.settings import get_settings
from inkspire_api.throttle import RateLimiter

# --- the exact prompt sent, pinned alongside every other shape (test_llm.py) ----


@pytest.mark.parametrize("case", sorted(COMPLETION_PROMPTS))
def test_the_completion_prompt_is_rendered_exactly_as_recorded(case: str) -> None:
    recorded = COMPLETION_PROMPTS[case]
    context = completions.CompletionContext(prefix=recorded["prefix"])
    assert completions.render_completion(context) == recorded["prompt"]


# --- assemble_completion -----------------------------------------------------


def test_assemble_completion_keeps_a_short_body_whole() -> None:
    body = "First paragraph.\n\nSecond paragraph."
    assert completions.assemble_completion(body, budget=1000).prefix == body


def test_assemble_completion_trims_from_the_tail_not_the_head() -> None:
    """Unlike a title or a context summary, what matters here is what the caret
    sits after."""
    paragraphs = [f"Paragraph {i} " + "x" * 50 for i in range(20)]
    body = "\n\n".join(paragraphs)

    trimmed = completions.assemble_completion(body, budget=200).prefix

    assert trimmed.endswith("x" * 50)
    assert "Paragraph 0 " not in trimmed
    assert len(trimmed) <= 200


# --- render_completion ---------------------------------------------------------


def test_render_completion_ends_on_the_prefix_s_own_last_character() -> None:
    context = completions.CompletionContext(prefix="She had not opened it.")
    assert completions.render_completion(context).endswith("She had not opened it.")


# --- clean_completion ----------------------------------------------------------


def test_clean_completion_keeps_a_leading_space() -> None:
    """The template asks the model to begin with a space when the text ends on a
    finished word -- stripping the front would break that join."""
    assert completions.clean_completion(" walked away.") == " walked away."


def test_clean_completion_keeps_a_leading_newline() -> None:
    assert completions.clean_completion("\nA new paragraph begins here.") == (
        "\nA new paragraph begins here."
    )


def test_clean_completion_keeps_no_leading_whitespace_when_there_is_none() -> None:
    assert completions.clean_completion("inished the word.") == "inished the word."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("She walked away. And then", "She walked away."),
        ("She walked away! Then", "She walked away!"),
        ("Did she walk away? Then", "Did she walk away?"),
        ('"She walked away." Then', '"She walked away."'),
    ],
)
def test_clean_completion_cuts_at_the_first_sentence_end(raw: str, expected: str) -> None:
    assert completions.clean_completion(raw) == expected


def test_clean_completion_with_no_sentence_end_is_kept_whole() -> None:
    assert completions.clean_completion(" and then she turned") == " and then she turned"


def test_clean_completion_strips_a_surrounding_quote_pair() -> None:
    assert completions.clean_completion('"She walked away."') == "She walked away."


def test_clean_completion_strips_a_continuation_label() -> None:
    assert completions.clean_completion("Continuation: she walked away.") == (
        "she walked away."
    )


def test_clean_completion_of_nothing_but_blank_is_empty() -> None:
    assert completions.clean_completion("   \n  ") == ""


def test_clean_completion_caps_at_max_completion_chars() -> None:
    raw = "x" * (completions.MAX_COMPLETION_CHARS + 50)
    assert len(completions.clean_completion(raw)) == completions.MAX_COMPLETION_CHARS


# --- the routes ------------------------------------------------------------------


@pytest.fixture
def completion_client(app, settings, tmp_path: Path, user):
    """A logged-in client whose provider answers from a transport given per test.

    No small model is needed here, unlike `title_client`/`summary_client` -- a
    completion asks whichever model the request names, which is the writer's own.
    """
    handlers: dict = {}

    def dispatch(request: httpx.Request) -> httpx.Response:
        return handlers["handler"](request)

    built = llm_service(tmp_path, dispatch, protocol="openai")
    app.dependency_overrides[llm.get_service] = lambda: built

    with TestClient(app) as client:
        client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
        client.handlers = handlers  # type: ignore[attr-defined]
        yield client


def note_id(client: TestClient) -> str:
    return client.get("/api/notes/tree").json()["files"][0]["id"]


def test_completing_requires_authentication(client: TestClient) -> None:
    assert client.post("/api/stories/file/x/complete", json={"model": "p/m"}).status_code == 401


@pytest.mark.parametrize("space", ["stories", "notes"])
def test_a_proposal_is_cleaned_and_returned(
    completion_client, data_root: Path, files_root: Path, space: str
) -> None:
    if space == "stories":
        make_story(
            data_root, "example-story", title="Example Story",
            chapters={"first-chapter.ink": "She had not opened it."},
        )
        file_id = chapter_id(completion_client, "Example Story", "first-chapter")
    else:
        make_note(files_root, "scratch.ink", "She had not opened it.")
        file_id = note_id(completion_client)

    completion_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body(" The wax still held.")
    )
    response = completion_client.post(
        f"/api/{space}/file/{file_id}/complete", json={"model": "p/model"}
    )
    assert response.status_code == 200
    assert response.json() == {"completion": " The wax still held."}


def test_the_chapter_s_own_text_reaches_the_prompt(
    completion_client, data_root: Path
) -> None:
    make_story(
        data_root, "example-story", title="Example Story",
        chapters={"first-chapter.ink": "She had not opened it."},
    )
    file_id = chapter_id(completion_client, "Example Story", "first-chapter")

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=answer_body(" The wax still held."))

    completion_client.handlers["handler"] = handler
    completion_client.post(
        f"/api/stories/file/{file_id}/complete", json={"model": "p/model"}
    )

    assert "She had not opened it." in seen["messages"][0]["content"]


def test_thinking_is_always_disabled(
    app, settings, tmp_path: Path, user, data_root: Path
) -> None:
    """A one-sentence proposal has no business reasoning out loud -- explicit
    here rather than left to whatever INKSPIRE_LLM_THINK happens to be. `think`
    reaches the payload only on the ollama path, so this needs that protocol."""
    make_story(
        data_root, "example-story", title="Example Story",
        chapters={"first-chapter.ink": "x"},
    )

    handlers: dict = {}

    def dispatch(request: httpx.Request) -> httpx.Response:
        return handlers["handler"](request)

    built = llm_service(tmp_path, dispatch, protocol="ollama", think=True)
    app.dependency_overrides[llm.get_service] = lambda: built

    with TestClient(app) as client:
        client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
        file_id = chapter_id(client, "Example Story", "first-chapter")

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, text=answer_body(" more.", native=True))

        handlers["handler"] = handler
        client.post(f"/api/stories/file/{file_id}/complete", json={"model": "p/model"})

    assert seen["think"] is False


def test_the_output_cap_is_sent_as_num_predict_on_ollama(
    app, settings, tmp_path: Path, user, data_root: Path
) -> None:
    make_story(
        data_root, "example-story", title="Example Story",
        chapters={"first-chapter.ink": "x"},
    )

    handlers: dict = {}

    def dispatch(request: httpx.Request) -> httpx.Response:
        return handlers["handler"](request)

    built = llm_service(tmp_path, dispatch, protocol="ollama")
    app.dependency_overrides[llm.get_service] = lambda: built
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"llm_completion_tokens": 5}
    )

    with TestClient(app) as client:
        client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
        file_id = chapter_id(client, "Example Story", "first-chapter")

        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(json.loads(request.content))
            return httpx.Response(200, text=answer_body(" more.", native=True))

        handlers["handler"] = handler
        client.post(f"/api/stories/file/{file_id}/complete", json={"model": "p/model"})

    assert seen["options"]["num_predict"] == 5


def test_the_output_cap_is_sent_as_max_tokens_on_openai(
    completion_client, settings, data_root: Path
) -> None:
    make_story(
        data_root, "example-story", title="Example Story",
        chapters={"first-chapter.ink": "x"},
    )
    file_id = chapter_id(completion_client, "Example Story", "first-chapter")
    completion_client.app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"llm_completion_tokens": 5}
    )

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=answer_body(" more."))

    completion_client.handlers["handler"] = handler
    completion_client.post(
        f"/api/stories/file/{file_id}/complete", json={"model": "p/model"}
    )

    assert seen["max_tokens"] == 5


@pytest.mark.parametrize("space", ["stories", "notes"])
def test_an_empty_file_is_refused(
    completion_client, data_root: Path, files_root: Path, space: str
) -> None:
    if space == "stories":
        make_story(
            data_root, "example-story", title="Example Story",
            chapters={"first-chapter.ink": ""},
        )
        file_id = chapter_id(completion_client, "Example Story", "first-chapter")
    else:
        make_note(files_root, "scratch.ink", "")
        file_id = note_id(completion_client)

    response = completion_client.post(
        f"/api/{space}/file/{file_id}/complete", json={"model": "p/model"}
    )
    assert response.status_code == 422
    assert "empty" in response.json()["message"].lower()


def test_an_unknown_model_is_a_422(completion_client, data_root: Path) -> None:
    make_story(
        data_root, "example-story", title="Example Story",
        chapters={"first-chapter.ink": "x"},
    )
    file_id = chapter_id(completion_client, "Example Story", "first-chapter")

    response = completion_client.post(
        f"/api/stories/file/{file_id}/complete", json={"model": "no-such-provider/m"}
    )
    assert response.status_code == 422


def test_a_provider_failure_is_a_500(completion_client, data_root: Path) -> None:
    make_story(
        data_root, "example-story", title="Example Story",
        chapters={"first-chapter.ink": "x"},
    )
    file_id = chapter_id(completion_client, "Example Story", "first-chapter")

    completion_client.handlers["handler"] = lambda request: httpx.Response(500, text="boom")
    response = completion_client.post(
        f"/api/stories/file/{file_id}/complete", json={"model": "p/model"}
    )
    assert response.status_code == 500


def test_nothing_usable_is_a_502(completion_client, data_root: Path) -> None:
    make_story(
        data_root, "example-story", title="Example Story",
        chapters={"first-chapter.ink": "x"},
    )
    file_id = chapter_id(completion_client, "Example Story", "first-chapter")

    completion_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("   ")
    )
    response = completion_client.post(
        f"/api/stories/file/{file_id}/complete", json={"model": "p/model"}
    )
    assert response.status_code == 502


def test_an_unknown_file_id_is_a_404(completion_client) -> None:
    response = completion_client.post(
        "/api/stories/file/does-not-exist/complete", json={"model": "p/model"}
    )
    assert response.status_code == 404


def test_an_exhausted_completion_budget_is_a_429(
    completion_client, app, data_root: Path
) -> None:
    make_story(
        data_root, "example-story", title="Example Story",
        chapters={"first-chapter.ink": "x"},
    )
    file_id = chapter_id(completion_client, "Example Story", "first-chapter")
    app.state.completion_limiter = RateLimiter(limit=1, interval=60)
    app.state.completion_limiter.consume(EMAIL)  # spend the one allowance up front

    response = completion_client.post(
        f"/api/stories/file/{file_id}/complete", json={"model": "p/model"}
    )
    assert response.status_code == 429


def test_the_completion_budget_is_separate_from_generations(
    completion_client, app, data_root: Path
) -> None:
    """A writer who has been generating heavily must not starve their own idle
    proposals -- `app.state.completion_limiter` is a budget of its own."""
    make_story(
        data_root, "example-story", title="Example Story",
        chapters={"first-chapter.ink": "x"},
    )
    file_id = chapter_id(completion_client, "Example Story", "first-chapter")
    app.state.llm_limiter = RateLimiter(limit=1, interval=60)
    app.state.llm_limiter.consume(EMAIL)  # exhaust generation's own budget
    app.state.completion_limiter = RateLimiter(limit=1, interval=60)  # untouched

    completion_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body(" more.")
    )
    response = completion_client.post(
        f"/api/stories/file/{file_id}/complete", json={"model": "p/model"}
    )
    assert response.status_code == 200
