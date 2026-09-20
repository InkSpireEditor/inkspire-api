# -*- coding: utf-8 -*-
"""Shared fixtures.

Each test gets its own SQLite file under tmp_path, so tests share no state and need
no cleanup step that could leave a row behind.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from inkspire_api.db import get_session
from inkspire_api.llm import LLMService
from inkspire_api.main import create_app
from inkspire_api.models import Base, RefreshToken, User, new_refresh_token, utcnow
from inkspire_api.security import hash_password
from inkspire_api.settings import Settings, get_settings

EMAIL = "test@example.com"
PASSWORD = "password"


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Test configuration. Explicit arguments win over the environment and `.env`.

    Throttling is off, or a test that logs in repeatedly would start getting 429s.
    `bcrypt_rounds=4` is the lowest bcrypt accepts: a secure work factor is wasted
    here and would dominate the runtime of the suite.
    """
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'data_test.db'}",
        data_root=tmp_path / "novel-data",
        files_root=tmp_path / "files",
        # At least 32 bytes, or PyJWT warns that the HMAC key is short for SHA-256.
        jwt_secret="test-only-signing-secret-padded-to-32-bytes",
        bcrypt_rounds=4,
        login_max_attempts=0,
    )


@pytest.fixture
def data_root(settings: Settings) -> Path:
    """An empty story repository, where the test settings expect one."""
    (settings.data_root / "stories").mkdir(parents=True)
    return settings.data_root


@pytest.fixture
def files_root(settings: Settings) -> Path:
    """An empty root for the files that are not a novel."""
    settings.files_root.mkdir(parents=True)
    return settings.files_root


def make_story(
    root: Path,
    slug: str,
    *,
    title: str | None = None,
    synopsis: str = "",
    chapters: dict[str, str] | None = None,
    listed: list | None = None,
    manifest: bool = True,
) -> Path:
    """Writes `stories/<slug>/` with a manifest and the chapter files given.

    `chapters` maps a filename to its content. `listed` is the manifest's `chapters:`
    entries, which are empty unless a test is about ordering or titles. `manifest=False`
    leaves out `story.yaml`, which is what makes a directory something other than a story.
    """
    story_dir = root / "stories" / slug
    (story_dir / "chapters").mkdir(parents=True)
    for filename, text in (chapters or {}).items():
        (story_dir / "chapters" / filename).write_text(text, encoding="utf-8")
    if manifest:
        (story_dir / "story.yaml").write_text(
            yaml.safe_dump(
                {
                    "title": title if title is not None else slug,
                    "synopsis": synopsis,
                    "chapters": listed or [],
                },
                sort_keys=False,
                allow_unicode=True,
            ),
            encoding="utf-8",
        )
    return story_dir


def entry_named(entries: list[dict], name: str) -> dict:
    """The entry in a listing shown under `name`.

    A listing is an array of entries each carrying its own `id`, so a test that wants one
    of them finds it by the name a reader would pick it out by.
    """
    for entry in entries:
        if entry["name"] == name:
            return entry
    raise AssertionError(f'Nothing named "{name}" in {[e["name"] for e in entries]}.')


def dir_named(client: TestClient, space: str, name: str) -> dict:
    """The directory `space`'s tree shows under `name`, with the files in it."""
    return entry_named(client.get(f"/api/{space}/tree").json()["dirs"], name)


def story_id(client: TestClient, name: str = "Example Story") -> str:
    """The id the tree gives the story shown under `name`."""
    return dir_named(client, "stories", name)["id"]


def chapter_id(client: TestClient, story: str, chapter: str) -> str:
    """The id the tree gives the chapter shown under `chapter`, in the story `story`."""
    return entry_named(dir_named(client, "stories", story)["files"], chapter)["id"]


def make_folder(
    root: Path, slug: str, *, title: str | None = None, context: str | None = None
) -> Path:
    """Writes `<slug>/` in the files root, with a manifest only where one is asked for.

    A folder needs no `manifest.yaml` to exist, so the default writes none — which is
    what most tests want to start from.
    """
    directory = root / slug
    directory.mkdir(parents=True)

    document: dict = {}
    if title is not None:
        document["title"] = title
    if context is not None:
        document["context"] = context
    if document:
        (directory / "manifest.yaml").write_text(
            yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return directory


def make_note(directory: Path, filename: str, text: str = "") -> Path:
    """Writes one `.ink` file, in whichever directory it is given."""
    path = directory / filename
    path.write_text(text, encoding="utf-8")
    return path


#: A whole timeline, small enough to assert on in full. Two characters with one event
#: each and one they share, so there is a crossing and an arrow per character, and one
#: arc spanning the lot. Placeholder names throughout.
#:
#: Typed as a bare `dict` because it is a YAML document: a test that breaks one field to
#: see what the route answers is copying this and assigning a value of another type.
TIMELINE: dict = {
    "title": "Example Timeline",
    "characters": {
        "alpha": {"name": "Jane Doe", "color": "blue"},
        "beta": {"name": "John Smith", "color": "red"},
    },
    "positions": {
        "alpha": {"characters": ["alpha"], "position": 0},
        "beta": {"characters": ["beta"], "position": 1},
        "both": {"characters": ["alpha", "beta"], "position": 2},
    },
    "events": {
        "first": {
            "date": "2001-01-01",
            "description": "First event",
            "characters": ["alpha"],
        },
        "second": {
            "date": "2001-02-01",
            "description": "Second event",
            "characters": ["alpha", "beta"],
            "dateStyle": "%Y-%m",
            "href": "https://example.com/",
        },
        "third": {
            "date": "2001-03-01",
            "description": "Third event",
            "characters": ["beta"],
        },
    },
    "arcs": {"opening": {"firstEvent": "first", "lastEvent": "third", "name": "Opening"}},
}


def make_timeline(story_dir: Path, document: dict | None = None) -> Path:
    """Writes a story's `timeline.yaml`. Pass `document` to write a broken one."""
    path = story_dir / "timeline.yaml"
    path.write_text(
        yaml.safe_dump(
            TIMELINE if document is None else document,
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def make_lorebook(
    story_dir: Path,
    *,
    title: str = "Example Lorebook",
    characters: dict[str, dict] | None = None,
    entities: dict[str, dict] | None = None,
) -> Path:
    """Writes a story's `lorebook/`: a manifest, an extension and the authored data.

    The defaults are one character with prose and one organisation she belongs to, which
    is enough for a graph with a node of each kind and an edge between them. The
    extension is minimal on purpose: `tests/lorebook/` is where the vocabulary itself is
    exercised, and these tests are about the routes.
    """
    directory = story_dir / "lorebook"
    (directory / "data" / "characters").mkdir(parents=True)

    (directory / "lorebook.yaml").write_text(
        yaml.safe_dump(
            {
                "title": title,
                "namespaces": {
                    "onto": "https://example.test/onto#",
                    "entity": "https://example.test/entity#",
                },
                "extension": "extension.yaml",
            },
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (directory / "extension.yaml").write_text(
        yaml.safe_dump(
            {"classes": ["Guild"], "object_properties": {"memberOf": {"range": "Guild"}}},
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (directory / "data" / "entities.yaml").write_text(
        yaml.safe_dump(
            entities
            if entities is not None
            else {"ExampleGuild": {"type": ["Organization", "Guild"], "name": "Example Guild"}},
            sort_keys=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )

    authored = (
        characters
        if characters is not None
        else {
            "Doe": {
                "id": "Doe",
                "type": "Character",
                "name": "Jane Doe",
                "memberOf": ["ExampleGuild"],
                "description": "A member of the Example Guild.",
                "sections": {"personality": "Steady.", "backstory": "From elsewhere."},
            }
        }
    )
    for local, record in authored.items():
        (directory / "data" / "characters" / f"{local.lower()}.yaml").write_text(
            yaml.safe_dump(record, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    return directory


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_engine(settings.database_url, future=True)
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    """A session for the test itself to inspect rows with."""
    with session_factory() as session:
        yield session


@pytest.fixture
def user(session_factory: sessionmaker[Session], settings: Settings) -> User:
    with session_factory() as session:
        user = User(
            email=EMAIL,
            roles=[],
            password=hash_password(PASSWORD, settings.bcrypt_rounds),
        )
        session.add(user)
        session.commit()
        return user


@pytest.fixture
def app(settings: Settings, session_factory: sessionmaker[Session]):
    application = create_app(settings)
    application.dependency_overrides[get_settings] = lambda: settings

    def _session() -> Iterator[Session]:
        with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = _session
    return application


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


@pytest.fixture
def logged_in(client: TestClient, user: User) -> TestClient:
    """A client holding the three cookies of a live session."""
    response = client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


def bearer(client: TestClient) -> dict[str, str]:
    """The Authorization header for a logged-in client, from a fresh login."""
    response = client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['token']}"}


def set_cookies(response) -> dict[str, dict[str, str]]:
    """Every Set-Cookie on a response, as {name: {attribute: value}}.

    Cookie flags are part of the contract, and httpx's cookie jar drops them, so
    the raw headers are parsed instead.
    """
    parsed: dict[str, dict[str, str]] = {}
    for header in response.headers.get_list("set-cookie"):
        first, *rest = [part.strip() for part in header.split(";")]
        name, _, value = first.partition("=")
        # A cleared cookie is sent as `name=""`, quotes included.
        attributes = {"value": value.strip('"')}
        for part in rest:
            key, _, attribute_value = part.partition("=")
            attributes[key.lower()] = attribute_value
        parsed[name] = attributes
    return parsed


def llm_settings(
    tmp_path: Path,
    *,
    protocol: str = "openai",
    think: bool | None = None,
    num_ctx: int | None = None,
    ttl: int = 3600,
) -> Settings:
    """Settings with one provider, named `p`, reachable at https://provider.test."""
    providers = tmp_path / "providers.yaml"
    providers.write_text(
        f"p:\n  url: https://provider.test\n  key: secret\n  protocol: {protocol}\n",
        encoding="utf-8",
    )
    return Settings(
        jwt_secret="x" * 32,
        llm_providers_file=providers,
        llm_think=think,
        llm_num_ctx=num_ctx,
        llm_cache_ttl=ttl,
    )


def llm_service(tmp_path: Path, handler, **kwargs) -> LLMService:
    """A service whose provider is answered by `handler` instead of over the network."""
    return LLMService(
        llm_settings(tmp_path, **kwargs), transport=httpx.MockTransport(handler)
    )


def sse_body(*events: dict) -> str:
    """A complete chat-completions response body, terminator included."""
    return "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"


def delta(text: str) -> dict:
    """One chat-completions content chunk."""
    return {"choices": [{"delta": {"content": text}, "finish_reason": None}]}


def add_refresh_token(
    session_factory: sessionmaker[Session], user: User, *, expires_in: int
) -> str:
    """Stores a refresh token expiring in `expires_in` seconds; a negative value is expired."""
    token = new_refresh_token()
    with session_factory() as session:
        session.add(
            RefreshToken(
                token=token,
                user_id=user.id,
                expires_at=utcnow() + datetime.timedelta(seconds=expires_in),
            )
        )
        session.commit()
    return token
