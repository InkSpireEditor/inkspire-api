# -*- coding: utf-8 -*-
"""Text generation.

`GET /api/llm/models` lists what every configured provider offers, prefixed with
the provider name. `POST /api/stories/file/{id}/generate` and the `/api/notes`
equivalent stream a continuation of a chapter's or a note's own text back as
server-sent events -- read fresh from disk, not uploaded by the client, so the
file is always what is asked about rather than whatever a tab happens to hold.
See `docs/prompt.md` for why assembly lives here and insertion stays with the
editor.

Two protocols are spoken, chosen per provider: the chat-completions API, and
Ollama's own. See `Protocol` for why both are needed.

The generated text is never written to disk here. The client inserts each delta
through its own editing command, so it lands on the browser's undo stack and is
marked as a model's at the moment it is inserted (`docs/prompt.md` has why); nothing
has to be reconciled between a write here and one there, because there is only
ever one.
"""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, NamedTuple

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import prompt as prompt_lib
from .context_summary import parse_section as parse_context_summary
from .deps import CurrentUser, SettingsDep
from .files import NotesDep, ScannerDep
from .fs import MAX_FILE_BYTES
from .settings import Settings
from .storage import Chapter

#: Upper bound on a model identifier, across every provider.
MAX_MODEL_LENGTH = 255

#: Listing models is a cheap metadata call and should fail fast rather than wait out
#: a generation-sized budget.
MODELS_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


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


@dataclasses.dataclass(frozen=True)
class GenerationOptions:
    """Everything one generation needs beyond the model and the text itself, resolved
    once per request rather than held on `LLMService`.

    `LLMService` is a singleton on `app.state` (`get_service`), so these cannot be its
    instance attributes the way they were before this existed: setting one from a
    request's override would leak that request's choice into the next one to use the
    same service. `LLMService.defaults` is the server's own configuration as one of
    these; `overlay` is how a request's per-call choices are layered onto it, and the
    result is what `stream`/`_payload` actually read.
    """

    temperature: float
    prompt_budget: int
    prefix_share: float
    num_ctx: int | None
    think: bool | None
    send_selection: bool

    @classmethod
    def from_settings(cls, settings: Settings) -> GenerationOptions:
        return cls(
            temperature=settings.llm_temperature,
            prompt_budget=settings.llm_prompt_budget,
            prefix_share=settings.llm_prefix_share,
            num_ctx=settings.llm_num_ctx,
            think=settings.llm_think,
            send_selection=settings.llm_send_selection,
        )

    def overlay(self, **overrides: Any) -> GenerationOptions:
        """`self`, with every field named in `overrides` whose value is not `None`
        replaced by that value. Omitting a field, or giving it `None`, leaves the
        server's own default -- or a previous override -- in place."""
        given = {name: value for name, value in overrides.items() if value is not None}
        return dataclasses.replace(self, **given)


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
        #: The server's own configuration, as one request's worth of settings. Never
        #: mutated after construction -- see `GenerationOptions`.
        self.defaults = GenerationOptions.from_settings(settings)
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
        # Carried so a client can tell which models can honour a request like `think`
        # at all -- the chat-completions protocol has no equivalent, native or not.
        protocol = config.protocol.value
        return [
            {"name": f"{provider}/{entry[field]}", "protocol": protocol}
            for entry in entries
            if isinstance(entry, dict) and entry.get(field)
        ]

    async def stream(
        self,
        model: str,
        text: str,
        *,
        cursor: prompt_lib.Cursor | None = None,
        selection: prompt_lib.CursorRange | None = None,
        options: GenerationOptions | None = None,
        synopsis: str = "",
        summary: str = "",
    ) -> AsyncGenerator[str, None]:
        """Yields content chunks as the provider produces them.

        `text` is the writer's own prose -- the whole file, split at `cursor` or
        `selection` into a prefix and a suffix (or, with neither, a prefix alone),
        each trimmed to its share of the budget, and wrapped in the continuation,
        fill-in-the-middle or rewrite instructions before anything is sent. At most
        one of `cursor`/`selection` may be given; `prompt_lib.assemble` is what
        raises if both are.

        `synopsis` (api#17) is the containing story's synopsis or notes folder's
        context, if the caller found one -- passed straight through to
        `prompt_lib.assemble`, which renders it above everything else when it is
        not empty.

        `summary` (api#18) is the file's own stored summary of what an *earlier*
        trim dropped, if any -- also passed straight through to `prompt_lib.assemble`,
        which renders it only when *this* request's own trim actually cuts the
        prefix.

        `options` is this one generation's settings -- temperature, the budget and
        its split, `num_ctx`, whether to think, and whether a rewrite sends the
        selected passage's own text -- defaulting to `self.defaults`,
        the server's own configuration, when not given. A caller with a request that
        may have overridden any of them resolves `self.defaults.overlay(...)` first
        and passes the result here; `think` reaches the provider only on the ollama
        path -- the chat-completions protocol has no equivalent, native or otherwise.

        Raises `LLMError` if the provider cannot be reached or rejects the request,
        `UnknownModel` for a model naming no configured provider, and
        `CursorOutOfRange` (or its `InvertedRange` subclass) for a cursor or a
        selection that does not address a character of `text`. Once chunks have
        started arriving, a failure raises mid-iteration.
        """
        config, name = self.parse_model(model)
        native = config.protocol is Protocol.ollama
        url = _chat_url(config, native=native)
        resolved = options if options is not None else self.defaults

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport
            ) as client:
                async with client.stream(
                    "POST",
                    url,
                    headers=headers(config),
                    json=self._payload(
                        name,
                        text,
                        native=native,
                        cursor=cursor,
                        selection=selection,
                        options=resolved,
                        synopsis=synopsis,
                        summary=summary,
                    ),
                ) as response:
                    await _raise_for_status(response)
                    async for chunk in _events(response, native=native):
                        yield chunk
        except httpx.HTTPError as error:
            raise LLMError(f"Provider is unreachable: {error}") from error

    async def complete(
        self,
        model: str,
        prompt: str,
        *,
        options: GenerationOptions | None = None,
    ) -> str:
        """Answers `prompt` in one response, for something short enough that
        streaming it would buy nothing -- a proposed chapter title, today.

        `prompt` arrives already rendered: unlike `stream`, this knows nothing about
        `PromptContext` or any template, since what a title call sends is not the
        continuation/fill-in-the-middle/rewrite prompt `prompt_lib.assemble` builds.
        `options` defaults to `self.defaults` the same way `stream`'s does.

        Raises `LLMError` if the provider cannot be reached, rejects the request, or
        answers nothing usable, and `UnknownModel` for a model naming no configured
        provider.
        """
        config, name = self.parse_model(model)
        native = config.protocol is Protocol.ollama
        url = _chat_url(config, native=native)
        resolved = options if options is not None else self.defaults

        try:
            async with httpx.AsyncClient(
                timeout=self.timeout, transport=self.transport
            ) as client:
                response = await client.post(
                    url,
                    headers=headers(config),
                    json=self._chat_payload(
                        name, prompt, native=native, options=resolved, stream=False
                    ),
                )
                await _raise_for_status(response)
                return _answer(response.json(), native=native)
        except httpx.HTTPError as error:
            raise LLMError(f"Provider is unreachable: {error}") from error

    def _payload(
        self,
        model: str,
        text: str,
        *,
        native: bool,
        options: GenerationOptions,
        cursor: prompt_lib.Cursor | None = None,
        selection: prompt_lib.CursorRange | None = None,
        synopsis: str = "",
        summary: str = "",
    ) -> dict[str, Any]:
        context = prompt_lib.assemble(
            text,
            budget=options.prompt_budget,
            cursor=cursor,
            selection=selection,
            prefix_share=options.prefix_share,
            send_selection=options.send_selection,
            synopsis=synopsis,
            summary=summary,
        )
        rendered = prompt_lib.render(context)
        return self._chat_payload(model, rendered, native=native, options=options, stream=True)

    def _chat_payload(
        self,
        model: str,
        rendered: str,
        *,
        native: bool,
        options: GenerationOptions,
        stream: bool,
    ) -> dict[str, Any]:
        """The provider payload for one already-rendered prompt.

        `stream` is `True` from `_payload` (a generation) and `False` from
        `complete` (a one-shot answer) -- the only difference between the two call
        sites, since everything below is about the protocol, not about what is being
        asked.
        """
        messages = [{"role": "user", "content": rendered}]
        if not native:
            payload: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "stream": stream,
                "temperature": options.temperature,
            }
            return payload

        # Ollama takes sampling settings under "options" rather than at the top level,
        # and `think` beside them. num_ctx is sent only when configured, so the model's
        # own default applies otherwise. `think` left unset (neither a per-request
        # override nor a server default) is left out of the payload entirely, which is
        # what a model with no reasoning mode expects.
        sampling: dict[str, Any] = {"temperature": options.temperature}
        if options.num_ctx is not None:
            sampling["num_ctx"] = options.num_ctx
        payload = {
            "model": model,
            "messages": messages,
            "stream": stream,
            "options": sampling,
        }
        if options.think is not None:
            payload["think"] = options.think
        return payload


class Event(NamedTuple):
    """One decoded chunk: text to append, and why generation stopped if it did.

    Reasoning is deliberately not carried. A model that thinks emits it on a separate
    field, and it is not part of what the writer is composing.
    """

    content: str = ""
    done_reason: str | None = None


async def _raise_for_status(response: httpx.Response) -> None:
    """Raises `LLMError` for a non-200 response, naming the provider's own detail."""
    if response.status_code != 200:
        detail = (await response.aread()).decode("utf-8", "replace")
        raise LLMError(f"Provider returned HTTP {response.status_code}: {detail[:500]}")


async def _events(response: httpx.Response, *, native: bool) -> AsyncGenerator[str, None]:
    """Decodes `response`'s lines into content chunks, in whichever protocol `native`
    selects, raising if the model spent its whole context window thinking.

    Extracted out of `stream` so the decode loop's own locals -- the line, the
    decoded event, and the two flags tracking whether anything was produced and why
    it stopped -- are not also `stream`'s locals.
    """
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
        # The model spent its whole context window on reasoning and never answered.
        # Reporting nothing would look like a working provider returning an empty
        # continuation.
        raise LLMError(
            "The model reached its context limit while thinking and wrote "
            "nothing. Disable thinking for it, or raise INKSPIRE_LLM_NUM_CTX."
        )


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


def _answer(payload: dict[str, Any], *, native: bool) -> str:
    """The text of one non-streamed chat response, native Ollama or chat-completions.

    Raises `LLMError` for a shape with neither -- `complete`'s equivalent of `_events`
    raising for a stream that produced nothing.
    """
    if native:
        content = (payload.get("message") or {}).get("content")
    else:
        choices = payload.get("choices") or []
        content = (choices[0].get("message") or {}).get("content") if choices else None
    if not content:
        raise LLMError("The model answered with nothing usable.")
    return content


def endpoint(base_url: str, path: str) -> str:
    """Builds an endpoint URL, tolerating a base that already ends in `/v1` or `/`."""
    base = base_url.rstrip("/").removesuffix("/v1")
    return f"{base}/v1/{path}"


def _chat_url(config: ProviderConfig, *, native: bool) -> str:
    """The chat endpoint for `config`, used by both `stream` and `complete`."""
    return f"{config.url.rstrip('/')}/api/chat" if native else endpoint(config.url, "chat/completions")


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
stories_router = APIRouter(prefix="/stories", tags=["stories"])
notes_router = APIRouter(prefix="/notes", tags=["notes"])


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
    """What a generation needs beyond the file itself, which the server reads fresh.

    `think`, `temperature`, `prompt_budget`, `prefix_share` and `num_ctx` each
    override the server's own default (`GenerationOptions`, `/api/llm/defaults`) for
    this request alone, when given; `None` -- the default for every one of them --
    leaves whichever default stands. None of this reaches `throttle.py`'s rate limit
    or the provider timeout: those are infrastructure, not a writing choice, and are
    not settings a request gets to raise for itself.
    """

    model: str = Field(max_length=MAX_MODEL_LENGTH)
    think: bool | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    prompt_budget: int | None = Field(default=None, gt=0, le=MAX_FILE_BYTES)
    prefix_share: float | None = Field(default=None, ge=0.0, le=1.0)
    num_ctx: int | None = Field(default=None, gt=0)
    #: The caret, or a selection's start: which paragraph it is in, and the offset
    #: within it (api#14). Both absent means the end of the file -- today's only
    #: behaviour, and a continuation either way. One without the other is rejected by
    #: `_anchor_from_request` rather than silently treated as "no caret".
    cursor_para: int | None = Field(default=None, ge=0)
    cursor_offset: int | None = Field(default=None, ge=0)
    #: A selection's end (api#20) -- given together, `cursor_para`/`cursor_offset`
    #: become its start instead of a caret, and the model is asked to rewrite what is
    #: between the two rather than continue or fill in around a single point. Absent
    #: with `cursor_para`/`cursor_offset` present is a caret, which is most requests.
    cursor_end_para: int | None = Field(default=None, ge=0)
    cursor_end_offset: int | None = Field(default=None, ge=0)
    #: Whether a rewrite sends the selected passage's own text, rather than only its
    #: word count. `None` -- the default -- leaves the server's own setting
    #: (`INKSPIRE_LLM_SEND_SELECTION`, on by default) in place; ignored outright for a
    #: continuation or a fill-in-the-middle, which have no passage to send.
    send_selection: bool | None = None

    def options(self, defaults: GenerationOptions) -> GenerationOptions:
        """`defaults` overlaid with whichever of this request's fields are not `None`."""
        return defaults.overlay(
            temperature=self.temperature,
            prompt_budget=self.prompt_budget,
            prefix_share=self.prefix_share,
            num_ctx=self.num_ctx,
            think=self.think,
            send_selection=self.send_selection,
        )


@router.get("/models")
async def list_models(service: ServiceDep) -> list[dict[str, str]]:
    try:
        return await service.models()
    except ValueError as error:  # a provider file that cannot be read
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR, str(error)
        ) from error


@router.get("/defaults")
async def get_defaults(service: ServiceDep) -> GenerationOptions:
    """The server's own generation settings, so a client can initialise a settings
    panel against them and has something to reset to.

    Named *defaults* rather than *settings*: nothing here is stored per writer, and a
    per-request override in `GenerateRequest` never changes what this answers.
    """
    return service.defaults


@router.get("/features")
async def get_features(settings: SettingsDep) -> dict[str, bool]:
    """Which optional, small-model-backed features this installation offers.

    Not a field on `/defaults`, which answers `GenerationOptions` -- a capability is
    not one generation's setting, and adding one here would change that pinned
    response shape for something unrelated to it. `title` and `summary` are both on
    once `INKSPIRE_LLM_SMALL_MODEL` is configured -- one setting governs both
    features, so one condition answers both keys.
    """
    small_model_configured = settings.llm_small_model is not None
    return {"title": small_model_configured, "summary": small_model_configured}


def _stored_summary(section: str | None) -> str:
    """A file's stored context-summary section (api#18) as the plain text
    `_stream_generation` passes down, or `""` where there is none or it cannot be
    read -- the same "nothing stored" reading `context_summary.parse_section` itself
    gives an unreadable section. `prompt.assemble` decides on its own whether this
    request's own trim is what actually renders it.
    """
    if section is None:
        return ""
    parsed = parse_context_summary(section)
    return parsed.text if parsed is not None else ""


EMPTY_FILE = "Nothing to continue: the file is empty."
SPLIT_CURSOR = "cursor_para and cursor_offset must be given together, or not at all."
SPLIT_CURSOR_END = (
    "cursor_end_para and cursor_end_offset must be given together, or not at all."
)
DANGLING_CURSOR_END = (
    "cursor_end_para/cursor_end_offset were given without cursor_para/cursor_offset, "
    "which would be a selection with no start."
)


class Anchor(NamedTuple):
    """Either half of a request's anchor, resolved to one shape for `assemble`."""

    cursor: prompt_lib.Cursor | None
    selection: prompt_lib.CursorRange | None


def _anchor_from_request(body: GenerateRequest) -> Anchor:
    """The request's caret or selection, or neither for the end of the file.

    Raises a 422 for either pair split across one field -- silently treating that as
    "no caret"/"no selection" would generate somewhere other than what was asked --
    and for an end given with no start, which would otherwise describe a selection
    with nowhere to begin.
    """
    if body.cursor_para is None and body.cursor_offset is None:
        if body.cursor_end_para is not None or body.cursor_end_offset is not None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, DANGLING_CURSOR_END)
        return Anchor(cursor=None, selection=None)
    if body.cursor_para is None or body.cursor_offset is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, SPLIT_CURSOR)

    start = prompt_lib.Cursor(para=body.cursor_para, offset=body.cursor_offset)
    if body.cursor_end_para is None and body.cursor_end_offset is None:
        return Anchor(cursor=start, selection=None)
    if body.cursor_end_para is None or body.cursor_end_offset is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, SPLIT_CURSOR_END)

    end = prompt_lib.Cursor(para=body.cursor_end_para, offset=body.cursor_end_offset)
    return Anchor(cursor=None, selection=prompt_lib.CursorRange(start, end))


async def _stream_generation(
    text: str,
    body: GenerateRequest,
    request: Request,
    user: CurrentUser,
    service: LLMService,
    *,
    synopsis: str = "",
    summary: str = "",
) -> StreamingResponse:
    """Shared by both spaces: rate-limits, starts the stream, and turns a failure
    before the first chunk into an ordinary status code.

    `synopsis` (api#17) is the containing story's synopsis or notes folder's
    context, looked up by the caller -- this function knows nothing about either
    root, so it only ever passes the string along. `summary` (api#18) is the
    file's own stored context summary, looked up by the caller the same way.

    Each event is `{"delta": "..."}` with text to append, `{"error": "..."}` if the
    provider fails once chunks have already been sent, and `[DONE]` at the end.

    A failure before the first chunk is an ordinary status code instead, which is why
    the first chunk is awaited here rather than inside the response body: at that
    point no bytes have been sent and a status code is still possible.
    """
    if not request.app.state.llm_limiter.consume(user.email):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many requests")

    anchor = _anchor_from_request(body)
    options = body.options(service.defaults)

    # Calling an async generator function runs none of its body, so the model check,
    # the cursor/selection check and the connection all happen on this first step.
    chunks = service.stream(
        body.model,
        text,
        cursor=anchor.cursor,
        selection=anchor.selection,
        options=options,
        synopsis=synopsis,
        summary=summary,
    )
    try:
        first = await anext(chunks, None)
    except UnknownModel as error:
        await chunks.aclose()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(error)) from error
    except prompt_lib.CursorOutOfRange as error:
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


@stories_router.post("/file/{file_id}/generate")
async def generate_story(
    file_id: str,
    body: GenerateRequest,
    *,
    request: Request,
    user: CurrentUser,
    scanner: ScannerDep,
    service: ServiceDep,
) -> StreamingResponse:
    """Streams a continuation of a chapter's or a one-shot's own text.

    Read fresh from disk rather than taken from the request: the file is the source
    of truth once the editor has saved, and the client no longer uploads its contents
    for this. See `docs/prompt.md` for why saving before calling this is the editor's
    responsibility, not something checked here.
    """
    file = scanner.file(file_id)
    document = scanner.read_document(file_id)
    text = document.body
    if not text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, EMPTY_FILE)
    # A one-shot is a bare File, never a Chapter -- it belongs to no story, so it
    # has no synopsis to offer (api#17).
    synopsis = scanner.story(file.story_id).summary if isinstance(file, Chapter) else ""
    summary = _stored_summary(document.context_summary)
    return await _stream_generation(
        text, body, request, user, service, synopsis=synopsis, summary=summary
    )


@notes_router.post("/file/{file_id}/generate")
async def generate_note(
    file_id: str,
    body: GenerateRequest,
    *,
    request: Request,
    user: CurrentUser,
    notes: NotesDep,
    service: ServiceDep,
) -> StreamingResponse:
    """The same, for a note."""
    note = notes.note(file_id)
    document = notes.read_document(file_id)
    text = document.body
    if not text.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, EMPTY_FILE)
    # A root-level note belongs to no folder, so it has no context to offer (api#17).
    synopsis = notes.folder(note.folder_id).summary if note.folder_id is not None else ""
    summary = _stored_summary(document.context_summary)
    return await _stream_generation(
        text, body, request, user, service, synopsis=synopsis, summary=summary
    )
