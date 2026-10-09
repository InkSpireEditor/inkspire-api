# -*- coding: utf-8 -*-
"""`clean_title`, `assemble_title`, and the `POST .../file/{id}/title` route.

The route's own prompt rendering is pinned in `tests/data/prompts.json` through
`test_llm.py`'s parametrised test, the same way the continuation/rewrite prompts are
-- this file is about the route's behaviour, not the exact wording it sends.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import EMAIL, PASSWORD, answer_body, chapter_id, llm_service, make_story
from tests.test_llm import TITLE_PROMPTS

from inkspire_api import llm, titles
from inkspire_api.settings import get_settings
from inkspire_api.throttle import RateLimiter

# --- the exact prompt sent, pinned alongside every other shape (test_llm.py) -----


@pytest.mark.parametrize("case", sorted(TITLE_PROMPTS))
def test_the_title_prompt_is_rendered_exactly_as_recorded(case: str) -> None:
    recorded = TITLE_PROMPTS[case]
    context = titles.TitleContext(
        prose=recorded["prose"],
        current_title=recorded.get("current_title", ""),
        instruction=recorded.get("instruction", ""),
    )
    assert titles.render_title(context) == recorded["prompt"]

# --- the current title, so a reroll does not just echo the first answer -----


def test_current_title_adds_a_paragraph_naming_it() -> None:
    context = titles.TitleContext(prose="She had not opened it.", current_title="Old Title")
    rendered = titles.render_title(context)
    assert 'Its current title is "Old Title"' in rendered


def test_no_current_title_adds_nothing() -> None:
    with_blank = titles.render_title(titles.TitleContext(prose="She had not opened it."))
    with_explicit_empty = titles.render_title(
        titles.TitleContext(prose="She had not opened it.", current_title="")
    )
    assert "current title" not in with_blank
    assert with_blank == with_explicit_empty


def test_assemble_title_carries_the_current_title_through() -> None:
    context = titles.assemble_title("Some prose.", budget=1000, current_title="Old Title")
    assert context.current_title == "Old Title"


# --- the writer's own instruction, for when the plain roll isn't satisfying --


def test_instruction_adds_a_paragraph_naming_it() -> None:
    context = titles.TitleContext(prose="She had not opened it.", instruction="make it ominous")
    rendered = titles.render_title(context)
    assert "The writer adds: make it ominous" in rendered


def test_no_instruction_adds_nothing() -> None:
    with_blank = titles.render_title(titles.TitleContext(prose="She had not opened it."))
    with_explicit_empty = titles.render_title(
        titles.TitleContext(prose="She had not opened it.", instruction="")
    )
    assert "writer adds" not in with_blank
    assert with_blank == with_explicit_empty


def test_current_title_and_instruction_both_render_separated_by_one_blank_line() -> None:
    context = titles.TitleContext(
        prose="She had not opened it.", current_title="Old Title", instruction="make it ominous"
    )
    rendered = titles.render_title(context)
    assert '"Old Title"' in rendered
    assert "The writer adds: make it ominous" in rendered
    # Exactly one blank line between the two paragraphs, and nothing else between
    # them -- the thing a second, independent {% if %} block in the template
    # could not reliably produce (docs/prompt.md).
    assert "keep it intact.\n\nThe writer adds:" in rendered


def test_assemble_title_carries_the_instruction_through_and_strips_it() -> None:
    context = titles.assemble_title("Some prose.", budget=1000, instruction="  make it ominous  ")
    assert context.instruction == "make it ominous"


# --- clean_title -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('"The Wax Still Held"', "The Wax Still Held"),
        ("'The Wax Still Held'", "The Wax Still Held"),
        ("“The Wax Still Held”", "The Wax Still Held"),
        ("Title: The Wax Still Held", "The Wax Still Held"),
        ("Chapter Title: The Wax Still Held", "The Wax Still Held"),
        ("The Wax Still Held.", "The Wax Still Held"),
        ("The   Wax\tStill  Held", "The Wax Still Held"),
    ],
)
def test_clean_title_strips_what_a_model_reliably_adds(raw: str, expected: str) -> None:
    assert titles.clean_title(raw) == expected


def test_clean_title_takes_only_the_first_line_of_several_alternatives() -> None:
    raw = "The Wax Still Held\nThree Years Unopened\nThe Letter in the Study"
    assert titles.clean_title(raw) == "The Wax Still Held"


def test_clean_title_of_nothing_but_blank_lines_is_empty() -> None:
    assert titles.clean_title("   \n\n  \n") == ""


def test_clean_title_caps_at_the_same_length_a_filename_gets() -> None:
    from inkspire_api.fs import MAX_NAME_LENGTH

    raw = "x" * (MAX_NAME_LENGTH + 50)
    assert len(titles.clean_title(raw)) == MAX_NAME_LENGTH


# --- assemble_title ----------------------------------------------------------


def test_assemble_title_keeps_a_short_body_whole() -> None:
    body = "First paragraph.\n\nSecond paragraph."
    assert titles.assemble_title(body, budget=1000).prose == body


def test_assemble_title_trims_from_the_head_not_the_tail() -> None:
    paragraphs = [f"Paragraph {i} " + "x" * 50 for i in range(20)]
    body = "\n\n".join(paragraphs)

    trimmed = titles.assemble_title(body, budget=200).prose

    assert trimmed.startswith("Paragraph 0")
    assert len(trimmed) <= 200


# --- render_title --------------------------------------------------------------


def test_render_title_ends_on_the_chapter_s_own_last_character() -> None:
    context = titles.TitleContext(prose="She had not opened it.")
    assert titles.render_title(context).endswith("She had not opened it.")


# --- the route -----------------------------------------------------------------


@pytest.fixture
def title_client(app, settings, tmp_path: Path, user):
    """A logged-in client with a small model configured, whose provider answers
    from a transport given per test."""
    handlers: dict = {}

    def dispatch(request: httpx.Request) -> httpx.Response:
        return handlers["handler"](request)

    built = llm_service(tmp_path, dispatch, protocol="openai")
    app.dependency_overrides[llm.get_service] = lambda: built
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"llm_small_model": "p/model"}
    )

    with TestClient(app) as client:
        client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
        client.handlers = handlers  # type: ignore[attr-defined]
        yield client


def test_proposing_a_title_requires_authentication(client: TestClient) -> None:
    assert client.post("/api/stories/file/x/title").status_code == 401


def test_a_proposed_title_is_cleaned_and_returned(title_client, data_root: Path) -> None:
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")

    title_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body('"The Wax Still Held"')
    )
    response = title_client.post(f"/api/stories/file/{file_id}/title")
    assert response.status_code == 200
    assert response.json() == {"title": "The Wax Still Held"}


def test_the_chapter_s_own_current_name_reaches_the_prompt(
    title_client, data_root: Path
) -> None:
    """No `title` is set in the header, so the chapter is shown under its filename
    stem -- `scanner.file(...).name` is what the route must read, not the body."""
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=answer_body("A Title"))

    title_client.handlers["handler"] = handler
    title_client.post(f"/api/stories/file/{file_id}/title")

    assert "first-chapter" in seen["messages"][0]["content"]


def test_no_small_model_refuses_with_409(logged_in: TestClient, data_root: Path) -> None:
    """The plain `settings` fixture has no small model configured -- no provider is
    reachable from this client at all, which is fine: the check happens first."""
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(logged_in, "Example Story", "first-chapter")

    response = logged_in.post(f"/api/stories/file/{file_id}/title")
    assert response.status_code == 409
    assert "INKSPIRE_LLM_SMALL_MODEL" in response.json()["message"]


def test_an_empty_chapter_is_refused(title_client, data_root: Path) -> None:
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": ""}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")

    response = title_client.post(f"/api/stories/file/{file_id}/title")
    assert response.status_code == 422
    assert "empty" in response.json()["message"].lower()


def test_an_unknown_file_id_is_a_404(title_client) -> None:
    response = title_client.post("/api/stories/file/does-not-exist/title")
    assert response.status_code == 404


def test_a_bodyless_post_still_works(title_client, data_root: Path) -> None:
    """The dice button sends no body at all -- `body` must default, not be required."""
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")

    title_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("A Title")
    )
    response = title_client.post(f"/api/stories/file/{file_id}/title")
    assert response.status_code == 200


def test_the_instruction_reaches_the_prompt(title_client, data_root: Path) -> None:
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=answer_body("A Title"))

    title_client.handlers["handler"] = handler
    title_client.post(
        f"/api/stories/file/{file_id}/title", json={"instruction": "make it ominous"}
    )

    assert "The writer adds: make it ominous" in seen["messages"][0]["content"]


def test_an_overlong_instruction_is_rejected(title_client, data_root: Path) -> None:
    """A `Field(max_length=...)` violation is a malformed body, not a semantically
    wrong one -- 400, the same as any other request validation failure
    (`main.py`'s `RequestValidationError` handler), not 422."""
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")

    too_long = "x" * (titles.MAX_INSTRUCTION_LENGTH + 1)
    response = title_client.post(
        f"/api/stories/file/{file_id}/title", json={"instruction": too_long}
    )
    assert response.status_code == 400


def test_the_rate_limit_is_shared_with_generation(title_client, app, data_root: Path) -> None:
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")
    app.state.llm_limiter = RateLimiter(limit=1, interval=60)

    title_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("A Title")
    )
    first = title_client.post(f"/api/stories/file/{file_id}/title")
    assert first.status_code == 200

    second = title_client.post(f"/api/stories/file/{file_id}/title")
    assert second.status_code == 429


def test_an_unconfigured_small_model_provider_is_a_422(
    title_client, app, settings, data_root: Path
) -> None:
    """`llm_small_model` naming a provider absent from `config/providers.yaml`."""
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"llm_small_model": "absent/model"}
    )

    response = title_client.post(f"/api/stories/file/{file_id}/title")
    assert response.status_code == 422


def test_a_provider_failure_is_a_500(title_client, data_root: Path) -> None:
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")

    title_client.handlers["handler"] = lambda request: httpx.Response(500, text="boom")
    response = title_client.post(f"/api/stories/file/{file_id}/title")
    assert response.status_code == 500


def test_nothing_usable_from_the_model_is_a_502(title_client, data_root: Path) -> None:
    make_story(
        data_root, "example-story", title="Example Story", chapters={"first-chapter.ink": "x"}
    )
    file_id = chapter_id(title_client, "Example Story", "first-chapter")

    title_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("   ")
    )
    response = title_client.post(f"/api/stories/file/{file_id}/title")
    assert response.status_code == 502
