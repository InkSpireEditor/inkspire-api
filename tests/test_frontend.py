# -*- coding: utf-8 -*-
"""The built frontend served by the API, and the settings a deployment turns on."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.conftest import EMAIL, PASSWORD, set_cookies

from inkspire_api.db import get_session
from inkspire_api.frontend import CONTENT_SECURITY_POLICY
from inkspire_api.main import create_app
from inkspire_api.models import User
from inkspire_api.settings import Settings, get_settings

INDEX = "<!doctype html><title>Example</title>"


@pytest.fixture
def build(tmp_path: Path) -> Path:
    """A frontend build: an index, a hashed script, and a secret file outside it."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "index-abc123.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("not for the web", encoding="utf-8")
    return root


def serving(settings: Settings, session_factory, **changes) -> TestClient:
    configured = settings.model_copy(update=changes)
    application = create_app(configured)
    application.dependency_overrides[get_settings] = lambda: configured

    def _session():
        with session_factory() as session:
            yield session

    application.dependency_overrides[get_session] = _session
    return TestClient(application)


@pytest.fixture
def site(settings, session_factory, build) -> Iterator[TestClient]:
    with serving(settings, session_factory, frontend_dir=build) as client:
        yield client


def test_nothing_is_served_without_a_frontend_directory(client: TestClient) -> None:
    assert client.get("/").status_code == 404


def test_the_index_is_served_at_the_root(site: TestClient) -> None:
    response = site.get("/")
    assert response.status_code == 200
    assert response.text == INDEX


def test_a_frontend_route_loads_the_index(site: TestClient) -> None:
    for path in ("/stories/0123456789abcdef", "/notes/write/0123456789abcdef", "/nothing/here"):
        response = site.get(path)
        assert (response.status_code, response.text) == (200, INDEX), path


def test_an_asset_is_served_and_may_be_kept_for_good(site: TestClient) -> None:
    response = site.get("/assets/index-abc123.js")
    assert response.text == "console.log(1)"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_the_index_is_never_kept(site: TestClient) -> None:
    assert site.get("/").headers["cache-control"] == "no-cache"
    assert site.get("/stories/0123456789abcdef").headers["cache-control"] == "no-cache"


def test_every_answer_carries_the_content_security_policy(site: TestClient) -> None:
    for path in ("/", "/assets/index-abc123.js", "/stories/0123456789abcdef"):
        assert site.get(path).headers["content-security-policy"] == CONTENT_SECURITY_POLICY


@pytest.mark.parametrize(
    "path", ["/../outside.txt", "/%2e%2e/outside.txt", "/assets/../../outside.txt", "/..%2foutside.txt"]
)
def test_a_path_leaving_the_build_serves_the_index(site: TestClient, path: str) -> None:
    assert site.get(path).text == INDEX


def test_a_symlink_out_of_the_build_serves_the_index(site: TestClient, build: Path) -> None:
    (build / "link.txt").symlink_to(build.parent / "outside.txt")
    assert site.get("/link.txt").text == INDEX


def test_a_directory_serves_the_index(site: TestClient) -> None:
    assert site.get("/assets").text == INDEX


@pytest.mark.parametrize("path", ["/api", "/api/nope", "/auth/nope", "/api/stories/nope/more"])
def test_an_unknown_api_path_is_a_json_404(site: TestClient, path: str) -> None:
    response = site.get(path)
    assert response.status_code == 404
    assert response.json() == {"code": 404, "message": "Not found"}


def test_a_known_api_route_is_still_the_apis(site: TestClient) -> None:
    assert site.get("/api/me").status_code == 401


def test_only_reading_is_allowed(site: TestClient) -> None:
    assert site.post("/").status_code == 405
    assert site.put("/stories/0123456789abcdef").status_code == 405
    assert site.head("/").status_code == 200


def test_an_empty_directory_stops_the_app(settings, session_factory, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(RuntimeError, match="index.html"):
        serving(settings, session_factory, frontend_dir=empty)


# --- docs -----------------------------------------------------------------


def test_the_docs_are_on_by_default(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200


def test_the_docs_can_be_turned_off(settings, session_factory) -> None:
    with serving(settings, session_factory, expose_docs=False) as client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert client.get(path).status_code == 404, path


def test_the_docs_stay_off_beside_a_frontend(settings, session_factory, build) -> None:
    with serving(settings, session_factory, expose_docs=False, frontend_dir=build) as client:
        # The catch-all answers, and it answers the page, not the schema.
        assert client.get("/openapi.json").text == INDEX


# --- cookies --------------------------------------------------------------


def login(client: TestClient) -> dict[str, dict[str, str]]:
    response = client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return set_cookies(response)


def test_cookies_are_secure_over_http_when_the_settings_say_so(
    settings, session_factory, user: User
) -> None:
    with serving(settings, session_factory, cookie_secure=True) as client:
        cookies = login(client)
    assert len(cookies) == 3
    assert all("secure" in attributes for attributes in cookies.values())


def test_cookies_are_not_secure_over_http_by_default(client: TestClient, user: User) -> None:
    assert all("secure" not in attributes for attributes in login(client).values())


def test_cookies_can_be_forced_insecure_for_a_local_run(
    settings, session_factory, user: User
) -> None:
    with serving(settings, session_factory, cookie_secure=False) as client:
        client.base_url = client.base_url.copy_with(scheme="https")
        assert all("secure" not in attributes for attributes in login(client).values())


def test_logout_clears_cookies_as_secure_too(settings, session_factory, user: User) -> None:
    with serving(settings, session_factory, cookie_secure=True) as client:
        login(client)
        cleared = set_cookies(client.post("/auth/logout"))
    assert len(cleared) == 3
    assert all("secure" in attributes for attributes in cleared.values())


# --- secret files ---------------------------------------------------------


def test_the_signing_secret_is_read_from_a_secrets_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "INKSPIRE_JWT_SECRET").write_text("from-a-file", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INKSPIRE_JWT_SECRET", raising=False)
    monkeypatch.setenv("INKSPIRE_SECRETS_DIR", str(secrets))
    get_settings.cache_clear()
    try:
        assert get_settings().jwt_secret == "from-a-file"
    finally:
        get_settings.cache_clear()


def test_the_environment_wins_over_a_secret_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "INKSPIRE_JWT_SECRET").write_text("from-a-file", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INKSPIRE_JWT_SECRET", "from-the-environment")
    monkeypatch.setenv("INKSPIRE_SECRETS_DIR", str(secrets))
    get_settings.cache_clear()
    try:
        assert get_settings().jwt_secret == "from-the-environment"
    finally:
        get_settings.cache_clear()


def test_no_secrets_directory_leaves_the_settings_as_they_were(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("INKSPIRE_JWT_SECRET", raising=False)
    monkeypatch.delenv("INKSPIRE_SECRETS_DIR", raising=False)
    get_settings.cache_clear()
    try:
        assert get_settings().jwt_secret is None
    finally:
        get_settings.cache_clear()
