# -*- coding: utf-8 -*-
"""Provider configuration, the prompt, the two streaming protocols, and the endpoints.

Requests are answered by a transport built in the test rather than by a provider, so
these exercise the wire format both protocols produce without needing a model.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import EMAIL, PASSWORD, delta, llm_service, sse_body

from inkspire_api import llm
from inkspire_api.llm import (
    LLMError,
    LLMService,
    ModelCache,
    Protocol,
    UnknownModel,
    endpoint,
    load_providers,
    native_event,
    render_prompt,
    sse_event,
)
from inkspire_api.settings import Settings

#: The prompt sent for a set of inputs, recorded so a change to the template shows up
#: as a failure here rather than as a change in what the models are asked.
PROMPTS: dict[str, dict[str, str]] = json.loads(
    (Path(__file__).parent / "data" / "prompts.json").read_text(encoding="utf-8")
)

# --- the prompt ------------------------------------------------------------


def test_the_writers_text_is_not_html_escaped() -> None:
    """Escaping would send the model `&quot;` where the writer typed a quote."""
    rendered = render_prompt('She said "don\'t" & left.')
    assert 'She said "don\'t" & left.' in rendered
    assert "&quot;" not in rendered
    assert "&amp;" not in rendered


def test_the_prompt_ends_on_the_writers_last_character() -> None:
    """A trailing newline would tell the model the sentence had ended."""
    assert render_prompt("the house was").endswith("the house was")


@pytest.mark.parametrize("case", sorted(PROMPTS))
def test_the_prompt_is_rendered_exactly_as_recorded(case: str) -> None:
    """Pins the whole prompt, character for character, for ten kinds of input.

    `tests/data/prompts.json` holds the text sent to the model for each of them:
    quotes and ampersands that escaping would mangle, accented characters, CJK,
    Markdown, template delimiters typed by the writer, and text ending mid-word, on a
    space or on a blank line. Editing the template changes what every model is asked
    to do, so it fails here until the recorded prompt is updated with it.
    """
    recorded = PROMPTS[case]
    assert render_prompt(recorded["text"]) == recorded["prompt"]


def test_markdown_is_preserved_verbatim() -> None:
    text = "# Chapter\n\n*She* ran — and `stopped`."
    assert text in render_prompt(text)


def test_the_prompt_forbids_repeating_and_forbids_html() -> None:
    rendered = render_prompt("anything")
    assert "Never repeat" in rendered
    assert "Never emit HTML" in rendered


def test_the_writers_text_comes_last() -> None:
    """Instructions precede the text, so nothing is read as part of the story."""
    rendered = render_prompt("STORY")
    assert rendered.index("Existing text:") < rendered.index("STORY")


# --- provider configuration ------------------------------------------------


def write_providers(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def test_an_absent_provider_file_configures_nothing(tmp_path: Path) -> None:
    """Running with no provider is allowed; it just offers no model."""
    assert load_providers(tmp_path / "absent.yaml") == {}


def test_providers_are_read_with_openai_as_the_default_protocol(tmp_path: Path) -> None:
    path = write_providers(
        tmp_path / "providers.yaml",
        "hosted:\n  url: https://example.com/v1\n  key: secret\n",
    )
    providers = load_providers(path)
    assert providers["hosted"].url == "https://example.com/v1"
    assert providers["hosted"].key == "secret"
    assert providers["hosted"].protocol is Protocol.openai


def test_the_ollama_protocol_is_selected_explicitly(tmp_path: Path) -> None:
    path = write_providers(
        tmp_path / "providers.yaml",
        "local:\n  url: http://127.0.0.1:11434\n  key: null\n  protocol: ollama\n",
    )
    assert load_providers(path)["local"].protocol is Protocol.ollama


def test_a_provider_without_a_url_is_rejected_by_name(tmp_path: Path) -> None:
    path = write_providers(tmp_path / "providers.yaml", "broken:\n  key: secret\n")
    with pytest.raises(ValueError, match="broken"):
        load_providers(path)


def test_a_file_that_is_not_a_mapping_is_rejected(tmp_path: Path) -> None:
    path = write_providers(tmp_path / "providers.yaml", "- one\n- two\n")
    with pytest.raises(ValueError, match="mapping"):
        load_providers(path)


def test_an_empty_file_configures_nothing(tmp_path: Path) -> None:
    assert load_providers(write_providers(tmp_path / "providers.yaml", "")) == {}


# --- endpoint construction -------------------------------------------------


@pytest.mark.parametrize(
    "base",
    ["https://example.com", "https://example.com/", "https://example.com/v1", "https://example.com/v1/"],
)
def test_a_base_url_resolves_to_one_v1_path(base: str) -> None:
    """A base written with or without /v1 must not produce /v1/v1 or drop it."""
    assert endpoint(base, "models") == "https://example.com/v1/models"


# --- decoding both wire formats -------------------------------------------


def test_a_chat_completions_event_carries_its_delta() -> None:
    line = 'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}'
    assert sse_event(line) == llm.Event("Hello", None)


def test_a_chat_completions_reasoning_chunk_contributes_no_text() -> None:
    """A thinking model emits reasoning on its own field; it is not part of the story."""
    line = 'data: {"choices":[{"delta":{"content":"","reasoning":"hmm"}}]}'
    assert sse_event(line) == llm.Event("", None)


def test_the_terminator_and_keep_alives_decode_to_nothing() -> None:
    assert sse_event("data: [DONE]") is None
    assert sse_event("") is None
    assert sse_event(": keep-alive") is None
    assert sse_event("data: not json") is None


def test_a_finish_reason_is_carried_out_of_the_event() -> None:
    line = 'data: {"choices":[{"delta":{},"finish_reason":"length"}]}'
    assert sse_event(line) == llm.Event("", "length")


def test_a_native_event_carries_its_message_content() -> None:
    line = '{"message":{"role":"assistant","content":"Hello"},"done":false}'
    assert native_event(line) == llm.Event("Hello", None)


def test_native_thinking_contributes_no_text() -> None:
    line = '{"message":{"content":"","thinking":"working on it"},"done":false}'
    assert native_event(line) == llm.Event("", None)


def test_a_native_done_reason_is_carried_out_of_the_event() -> None:
    line = '{"message":{"content":""},"done":true,"done_reason":"length"}'
    assert native_event(line) == llm.Event("", "length")


def test_a_malformed_native_line_decodes_to_nothing() -> None:
    assert native_event("{not json") is None
    assert native_event("   ") is None


# --- the service, against a transport of our own ---------------------------


async def collect(iterator) -> list[str]:
    return [chunk async for chunk in iterator]


@pytest.mark.asyncio
async def test_an_unprefixed_model_is_refused_before_any_request(tmp_path: Path) -> None:
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    with pytest.raises(UnknownModel, match="prefixed"):
        await collect(llm_service(tmp_path, handler).stream("bare-name", "text"))


@pytest.mark.asyncio
async def test_an_unconfigured_provider_is_refused(tmp_path: Path) -> None:
    def handler(request):  # pragma: no cover - must not be reached
        raise AssertionError("no request should be made")

    with pytest.raises(UnknownModel, match="absent"):
        await collect(llm_service(tmp_path, handler).stream("absent/model", "text"))


@pytest.mark.asyncio
async def test_chat_completions_deltas_are_streamed_in_order(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["model"] == "model"
        assert body["temperature"] == 1.0
        assert request.headers["authorization"] == "Bearer secret"
        return httpx.Response(200, text=sse_body(delta("Hel"), delta("lo")))

    assert await collect(llm_service(tmp_path, handler).stream("p/model", "text")) == ["Hel", "lo"]


@pytest.mark.asyncio
async def test_the_native_protocol_posts_to_api_chat(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        body = json.loads(request.content)
        # Sampling settings belong under "options" for this protocol.
        assert body["options"]["temperature"] == 1.0
        assert "temperature" not in body
        return httpx.Response(
            200,
            text='{"message":{"content":"Hi"},"done":false}\n'
            '{"message":{"content":""},"done":true,"done_reason":"stop"}\n',
        )

    streamed = await collect(
        llm_service(tmp_path, handler, protocol="ollama").stream("p/model", "text")
    )
    assert streamed == ["Hi"]


@pytest.mark.asyncio
async def test_think_is_omitted_unless_configured(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text='{"message":{"content":"x"},"done":true}\n')

    await collect(llm_service(tmp_path, handler, protocol="ollama").stream("p/m", "t"))
    assert "think" not in seen


@pytest.mark.parametrize("think", [True, False])
@pytest.mark.asyncio
async def test_think_is_sent_when_configured(tmp_path: Path, think: bool) -> None:
    """Disabling reasoning has to reach the provider, or a thinking model stalls."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text='{"message":{"content":"x"},"done":true}\n')

    await collect(
        llm_service(tmp_path, handler, protocol="ollama", think=think).stream("p/m", "t")
    )
    assert seen["think"] is think


@pytest.mark.asyncio
async def test_num_ctx_is_sent_only_when_configured(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text='{"message":{"content":"x"},"done":true}\n')

    await collect(
        llm_service(tmp_path, handler, protocol="ollama", num_ctx=4096).stream("p/m", "t")
    )
    assert seen["options"]["num_ctx"] == 4096


@pytest.mark.asyncio
async def test_the_rendered_prompt_is_what_gets_sent(tmp_path: Path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=sse_body(delta("x")))

    await collect(llm_service(tmp_path, handler).stream("p/m", "the house was"))
    sent = seen["messages"][0]["content"]
    assert sent == render_prompt("the house was")
    assert sent.endswith("the house was")


@pytest.mark.asyncio
async def test_a_refused_request_raises_with_the_providers_words(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='{"error":"model not found"}')

    with pytest.raises(LLMError, match="404"):
        await collect(llm_service(tmp_path, handler).stream("p/m", "t"))


@pytest.mark.asyncio
async def test_an_unreachable_provider_raises(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(LLMError, match="unreachable"):
        await collect(llm_service(tmp_path, handler).stream("p/m", "t"))


@pytest.mark.asyncio
async def test_thinking_until_the_context_runs_out_is_an_error(tmp_path: Path) -> None:
    """Silence plus done_reason=length is a model that thought instead of answering.

    Reporting an empty continuation would look like a working provider with nothing
    to say, and the writer would have no idea why.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text='{"message":{"content":"","thinking":"..."},"done":false}\n'
            '{"message":{"content":""},"done":true,"done_reason":"length"}\n',
        )

    with pytest.raises(LLMError, match="context limit"):
        await collect(
            llm_service(tmp_path, handler, protocol="ollama").stream("p/m", "t")
        )


@pytest.mark.asyncio
async def test_an_empty_completion_without_that_reason_is_not_an_error(
    tmp_path: Path,
) -> None:
    """A model that simply had nothing to add is not a failure."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse_body())

    assert await collect(llm_service(tmp_path, handler).stream("p/m", "t")) == []


# --- listing models --------------------------------------------------------


@pytest.mark.asyncio
async def test_models_are_prefixed_with_their_provider(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        return httpx.Response(200, json={"data": [{"id": "one"}, {"id": "two"}]})

    assert await llm_service(tmp_path, handler).models() == [
        {"name": "p/one"},
        {"name": "p/two"},
    ]


@pytest.mark.asyncio
async def test_native_models_come_from_api_tags(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"model": "llama3:latest"}]})

    listed = await llm_service(tmp_path, handler, protocol="ollama").models()
    assert listed == [{"name": "p/llama3:latest"}]


@pytest.mark.asyncio
async def test_an_unreachable_provider_contributes_no_models(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    assert await llm_service(tmp_path, handler).models() == []


@pytest.mark.asyncio
async def test_a_provider_that_errors_contributes_no_models(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    assert await llm_service(tmp_path, handler).models() == []


@pytest.mark.asyncio
async def test_a_model_list_is_fetched_once_while_it_is_cached(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, json={"data": [{"id": "one"}]})

    built = llm_service(tmp_path, handler)
    await built.models()
    await built.models()
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_an_expired_model_list_is_fetched_again(tmp_path: Path) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url)
        return httpx.Response(200, json={"data": [{"id": "one"}]})

    built = llm_service(tmp_path, handler, ttl=0)
    await built.models()
    await built.models()
    assert len(calls) == 2


def test_a_cached_list_can_be_dropped() -> None:
    cache = ModelCache(ttl=3600)
    cache.put("p", [{"name": "p/one"}])
    cache.clear()
    assert cache.get("p") is None


# --- the endpoints ---------------------------------------------------------


@pytest.fixture
def llm_client(app, tmp_path: Path, user):
    """A logged-in client whose provider answers from a transport given per test."""
    handlers: dict = {}

    def dispatch(request: httpx.Request) -> httpx.Response:
        return handlers["handler"](request)

    built = llm_service(tmp_path, dispatch, protocol="openai")
    app.dependency_overrides[llm.get_service] = lambda: built

    with TestClient(app) as client:
        client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
        client.handlers = handlers  # type: ignore[attr-defined]
        yield client


def events(response) -> list[str]:
    """The payload of each `data:` line in a streamed response."""
    return [
        line.removeprefix("data:").strip()
        for line in response.text.splitlines()
        if line.startswith("data:")
    ]


def test_listing_models_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/llm/models").status_code == 401


def test_generating_requires_authentication(client: TestClient) -> None:
    response = client.post("/api/llm/generate", json={"model": "p/m", "prompt": "x"})
    assert response.status_code == 401


def test_the_models_endpoint_returns_the_prefixed_list(llm_client) -> None:
    llm_client.handlers["handler"] = lambda request: httpx.Response(
        200, json={"data": [{"id": "one"}]}
    )
    response = llm_client.get("/api/llm/models")
    assert response.status_code == 200
    assert response.json() == [{"name": "p/one"}]


def test_generation_streams_deltas_then_done(llm_client) -> None:
    llm_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=sse_body(delta("Hel"), delta("lo"))
    )
    response = llm_client.post(
        "/api/llm/generate", json={"model": "p/m", "prompt": "the house was"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # Proxy buffering would hold the chunks back and deliver them together at the end.
    assert response.headers["x-accel-buffering"] == "no"
    assert events(response) == ['{"delta": "Hel"}', '{"delta": "lo"}', "[DONE]"]


def test_a_provider_failure_before_any_text_is_a_status_code(llm_client) -> None:
    """Nothing has been sent yet, so this can still be an ordinary error response."""
    llm_client.handlers["handler"] = lambda request: httpx.Response(503, text="down")
    response = llm_client.post(
        "/api/llm/generate", json={"model": "p/m", "prompt": "x"}
    )
    assert response.status_code == 500
    assert "503" in response.json()["message"]


def test_a_failure_after_text_has_started_becomes_an_error_event(llm_client) -> None:
    """The status line is already sent by then, so the only way left is in the stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        async def chunks():
            yield b'data: {"choices":[{"delta":{"content":"Once"}}]}\n\n'
            raise httpx.ReadError("connection lost")

        return httpx.Response(200, content=chunks())

    llm_client.handlers["handler"] = handler
    response = llm_client.post(
        "/api/llm/generate", json={"model": "p/m", "prompt": "x"}
    )
    assert response.status_code == 200
    payloads = events(response)
    assert payloads[0] == '{"delta": "Once"}'
    assert "error" in payloads[1]
    assert payloads[-1] == "[DONE]"


def test_an_unknown_model_is_unprocessable(llm_client) -> None:
    llm_client.handlers["handler"] = lambda request: httpx.Response(200, text=sse_body())
    response = llm_client.post(
        "/api/llm/generate", json={"model": "absent/model", "prompt": "x"}
    )
    assert response.status_code == 422
    assert "absent" in response.json()["message"]


def test_an_overlong_prompt_is_rejected(llm_client) -> None:
    llm_client.handlers["handler"] = lambda request: httpx.Response(200, text=sse_body())
    response = llm_client.post(
        "/api/llm/generate",
        json={"model": "p/m", "prompt": "x" * (llm.MAX_PROMPT_LENGTH + 1)},
    )
    assert response.status_code == 400
    assert "prompt" in response.json()["message"]


def test_an_empty_prompt_is_rejected(llm_client) -> None:
    llm_client.handlers["handler"] = lambda request: httpx.Response(200, text=sse_body())
    response = llm_client.post("/api/llm/generate", json={"model": "p/m", "prompt": ""})
    assert response.status_code == 400


def test_generation_is_rate_limited_per_account(llm_client, app) -> None:
    """A generation costs a token whether or not the client reads the whole stream."""
    from inkspire_api.throttle import RateLimiter

    app.state.llm_limiter = RateLimiter(limit=2, interval=60)
    llm_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=sse_body(delta("x"))
    )

    for _ in range(2):
        assert (
            llm_client.post(
                "/api/llm/generate", json={"model": "p/m", "prompt": "x"}
            ).status_code
            == 200
        )

    refused = llm_client.post("/api/llm/generate", json={"model": "p/m", "prompt": "x"})
    assert refused.status_code == 429
    assert "message" in refused.json()
