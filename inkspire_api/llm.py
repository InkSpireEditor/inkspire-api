# -*- coding: utf-8 -*-
"""Text generation.

`GET /api/llm/models` lists what every configured provider offers, prefixed with
the provider name, and `POST /api/llm/generate` streams a continuation of the
writer's text back as server-sent events.

Two protocols are spoken, chosen per provider: the chat-completions API, and
Ollama's own. See `Protocol` for why both are needed.

The generated text is never written to disk here. The client owns the chapter it
is editing and saves it, so nothing has to be reconciled between the two.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NamedTuple

import httpx
import jinja2
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .deps import CurrentUser, SettingsDep
from .settings import Settings

#: Upper bound on a model identifier, across every provider.
MAX_MODEL_LENGTH = 255

#: Upper bound on one prompt. Well under the smallest context window a provider is
#: expected to offer, so the rendered template still fits alongside it.
MAX_PROMPT_LENGTH = 10000

#: Listing models is a cheap metadata call and should fail fast rather than wait out
#: a generation-sized budget.
MODELS_TIMEOUT = httpx.Timeout(10.0, connect=5.0)

PROMPT_TEMPLATE = "prompt.j2"

#: Autoescaping is off: the writer's apostrophes and quotes must reach the model as
#: typed, not as HTML entities.
JINJA = jinja2.Environment(
    loader=jinja2.PackageLoader("inkspire_api", "templates"),
    autoescape=False,
    keep_trailing_newline=False,
)


class Protocol(StrEnum):
    #: The chat-completions API, spoken by LiteLLM, Ollama's compatibility layer, and
    #: most hosted providers.
    openai = "openai"
    #: Ollama's own API. Needed to control reasoning: the compatibility layer accepts
    #: `think` and ignores it, so a reasoning model thinks whatever it is asked.
    ollama = "ollama"


class ProviderConfig(BaseModel):
    url: str
    key: str | None = None
    protocol: Protocol = Protocol.openai


class LLMError(RuntimeError):
    """A provider could not be reached, or answered with something unusable."""


class UnknownModel(ValueError):
    """The model identifier names no configured provider."""


def load_providers(path: Path) -> dict[str, ProviderConfig]:
    """Reads the provider file, which is absent on an installation with no provider.

    The file maps a provider name to its base URL, optional API key and protocol:

        local-ollama:
          url: http://127.0.0.1:11434
          key: null
          protocol: ollama

    The name becomes the prefix of every model identifier that provider offers.
    """
    if not path.is_file():
        return {}

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict):
        raise ValueError(f"{path} must contain a mapping of provider name to settings.")

    providers = {}
    for name, config in document.items():
        if not isinstance(config, dict) or "url" not in config:
            raise ValueError(
                f'{path}: provider "{name}" must be a mapping with a "url" key and an '
                'optional "key". Found: '
                f"{type(config).__name__}."
            )
        providers[str(name)] = ProviderConfig(**config)
    return providers


def render_prompt(text: str) -> str:
    """Wraps the writer's text in the continuation instructions sent to the model."""
    return JINJA.get_template(PROMPT_TEMPLATE).render(prompt=text)


class ModelCache:
    """Per-provider model lists, held for `ttl` seconds.

    The store is a dictionary in this process, so each worker fetches its own copy
    and a restart empties it.
    """

    def __init__(self, ttl: int) -> None:
        self.ttl = ttl
        self._entries: dict[str, tuple[float, list[dict[str, str]]]] = {}

    def get(self, provider: str) -> list[dict[str, str]] | None:
        entry = self._entries.get(provider)
        if entry is None:
            return None
        expires_at, models = entry
        if expires_at <= time.monotonic():
            del self._entries[provider]
            return None
        return models

    def put(self, provider: str, models: list[dict[str, str]]) -> None:
        self._entries[provider] = (time.monotonic() + self.ttl, models)

    def clear(self) -> None:
        self._entries.clear()


class LLMService:
    def __init__(
        self,
        settings: Settings,
        cache: ModelCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        #: Where requests are sent, when they should not go over the network.
        self.transport = transport
        self.providers = load_providers(settings.llm_providers_file)
        self.temperature = settings.llm_temperature
        self.num_ctx = settings.llm_num_ctx
        self.think = settings.llm_think
        # The idle timeout: the longest acceptable gap between two chunks. Because the
        # response is streamed, it does not have to cover a whole generation.
        self.timeout = httpx.Timeout(settings.llm_timeout, connect=10.0)
        self.cache = cache if cache is not None else ModelCache(settings.llm_cache_ttl)

    def parse_model(self, model: str) -> tuple[ProviderConfig, str]:
        provider, separator, name = model.partition("/")
        if not separator or not name:
            raise UnknownModel(
                f'Model "{model}" must be prefixed with a provider name, '
                'as in "my-provider/model-name".'
            )
        if provider not in self.providers:
            raise UnknownModel(f'Unknown provider "{provider}".')
        return self.providers[provider], name

    async def models(self) -> list[dict[str, str]]:
        """Every provider's models, prefixed with the provider name.

        A provider that cannot be reached contributes nothing rather than failing the
        whole list, so one dead endpoint does not hide the others.
        """
        collected: list[dict[str, str]] = []
        for name, config in self.providers.items():
            cached = self.cache.get(name)
            if cached is None:
                cached = await self._fetch_models(name, config)
                self.cache.put(name, cached)
            collected.extend(cached)
        return collected

    async def _fetch_models(
        self, provider: str, config: ProviderConfig
    ) -> list[dict[str, str]]:
        native = config.protocol is Protocol.ollama
        url = (
            f"{config.url.rstrip('/')}/api/tags"
            if native
            else endpoint(config.url, "models")
        )
        try:
            async with httpx.AsyncClient(
                timeout=MODELS_TIMEOUT, transport=self.transport
            ) as client:
                response = await client.get(url, headers=headers(config))
            if response.status_code != 200:
                return []
            payload = response.json()
        except (httpx.HTTPError, json.JSONDecodeError):
            return []

        # Ollama names a model under "models"/"model"; chat-completions under "data"/"id".
        entries = payload.get("models", []) if native else payload.get("data", [])
        field = "model" if native else "id"
        return [
            {"name": f"{provider}/{entry[field]}"}
            for entry in entries
            if isinstance(entry, dict) and entry.get(field)
        ]

    async def stream(self, model: str, prompt: str) -> AsyncGenerator[str, None]:
        """Yields content chunks as the provider produces them.

        Raises `LLMError` if the provider cannot be reached or rejects the request.
        Once chunks have started arriving, a failure raises mid-iteration.
        """
        config, name = self.parse_model(model)
        native = config.protocol is Protocol.ollama
        url = (
            f"{config.url.rstrip('/')}/api/chat"
            if native
            else endpoint(config.url, "chat/completions")
        )

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers(config),
                    json=self._payload(name, prompt, native=native),
                ) as response:
                    if response.status_code != 200:
                        detail = (await response.aread()).decode("utf-8", "replace")
                        raise LLMError(
                            f"Provider returned HTTP {response.status_code}: {detail[:500]}"
                        )

                    produced = False
                    reason = None
                    async for line in response.aiter_lines():
                        event = native_event(line) if native else sse_event(line)
                        if event is None:
                            continue
                        reason = event.done_reason or reason
                        if event.content:
                            produced = True
                            yield event.content

                    if not produced and reason == "length":
                        # The model spent its whole context window on reasoning and never
                        # answered. Reporting nothing would look like a working provider
                        # returning an empty continuation.
                        raise LLMError(
                            "The model reached its context limit while thinking and wrote "
                            "nothing. Disable thinking for it, or raise INKSPIRE_LLM_NUM_CTX."
                        )
        except httpx.HTTPError as error:
            raise LLMError(f"Provider is unreachable: {error}") from error

    def _payload(self, model: str, prompt: str, *, native: bool) -> dict[str, Any]:
        messages = [{"role": "user", "content": render_prompt(prompt)}]
        if not native:
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": True,
                "temperature": self.temperature,
            }
            return payload

        # Ollama takes sampling settings under "options" rather than at the top level,
        # and `think` beside them. num_ctx is sent only when configured, so the model's
        # own default applies otherwise.
        options: dict[str, Any] = {"temperature": self.temperature}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": options,
        }
        if self.think is not None:
            payload["think"] = self.think
        return payload


class Event(NamedTuple):
    """One decoded chunk: text to append, and why generation stopped if it did.

    Reasoning is deliberately not carried. A model that thinks emits it on a separate
    field, and it is not part of what the writer is composing.
    """

    content: str = ""
    done_reason: str | None = None


def sse_event(line: str) -> Event | None:
    """Decodes one chat-completions event line, or None for anything without content."""
    if not line.startswith("data:"):
        return None
    data = line.removeprefix("data:").strip()
    if not data or data == "[DONE]":
        return None
    try:
        payload = json.loads(data)
    except json.JSONDecodeError:
        return None

    content = ""
    reason = None
    for choice in payload.get("choices", []):
        content += (choice.get("delta") or {}).get("content") or ""
        reason = choice.get("finish_reason") or reason
    return Event(content, reason)


def native_event(line: str) -> Event | None:
    """Decodes one line of Ollama's own streaming format."""
    if not line.strip():
        return None
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        return None
    message = payload.get("message") or {}
    return Event(message.get("content") or "", payload.get("done_reason"))


def endpoint(base_url: str, path: str) -> str:
    """Builds an endpoint URL, tolerating a base that already ends in `/v1` or `/`."""
    base = base_url.rstrip("/").removesuffix("/v1")
    return f"{base}/v1/{path}"


def headers(config: ProviderConfig) -> dict[str, str]:
    sent = {"Content-Type": "application/json", "Accept": "application/json"}
    if config.key:
        sent["Authorization"] = f"Bearer {config.key}"
    return sent


def sse(payload: dict[str, str]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


DONE = "data: [DONE]\n\n"


# --- routes ----------------------------------------------------------------

router = APIRouter(prefix="/llm", tags=["llm"])


def get_service(request: Request, settings: SettingsDep) -> LLMService:
    """One service per application, so the model cache outlives a request.

    Built on first use rather than at startup: an unreadable provider file then
    answers 500 on the endpoint that needs it instead of preventing the API from
    starting at all.
    """
    if request.app.state.llm_service is None:
        request.app.state.llm_service = LLMService(settings)
    return request.app.state.llm_service


ServiceDep = Annotated[LLMService, Depends(get_service)]


class GenerateRequest(BaseModel):
    model: str = Field(max_length=MAX_MODEL_LENGTH)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_LENGTH)


@router.get("/models")
async def list_models(service: ServiceDep) -> list[dict[str, str]]:
    try:
        return await service.models()
    except ValueError as error:  # a provider file that cannot be read
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)
        ) from error


@router.post("/generate")
async def generate(
    body: GenerateRequest,
    request: Request,
    user: CurrentUser,
    service: ServiceDep,
) -> StreamingResponse:
    """Streams a continuation of the writer's text as server-sent events.

    Each event is `{"delta": "..."}` with text to append, `{"error": "..."}` if the
    provider fails once chunks have already been sent, and `[DONE]` at the end.

    A failure before the first chunk is an ordinary status code instead, which is why
    the first chunk is awaited here rather than inside the response body: at that
    point no bytes have been sent and a status code is still possible.
    """
    if not request.app.state.llm_limiter.consume(user.email):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    # Calling an async generator function runs none of its body, so both the model
    # check and the connection happen on this first step.
    chunks = service.stream(body.model, body.prompt)
    try:
        first = await anext(chunks, None)
    except UnknownModel as error:
        await chunks.aclose()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except LLMError as error:
        await chunks.aclose()
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)
        ) from error

    async def events() -> AsyncIterator[str]:
        try:
            if first is not None:
                yield sse({"delta": first})
                async for chunk in chunks:
                    yield sse({"delta": chunk})
        except LLMError as error:
            yield sse({"error": str(error)})
        finally:
            await chunks.aclose()
        yield DONE

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Without this a buffering reverse proxy holds the chunks back and
            # delivers them together at the end.
            "X-Accel-Buffering": "no",
        },
    )
