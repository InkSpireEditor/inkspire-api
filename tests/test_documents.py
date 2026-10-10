# -*- coding: utf-8 -*-
"""The editor's routes: prose and provenance together, on both roots.

`GET` recovers a stale paragraph from history before answering and writes nothing doing
it. `PUT` writes both parts in one go, because writing the prose alone would leave the
hashes describing text that is no longer there.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from git import Repo

from inkspire_api import llm, provenance, repository
from inkspire_api.fs import MAX_FILE_BYTES
from inkspire_api.settings import get_settings
from inkspire_api.throttle import RateLimiter
from tests.conftest import EMAIL, PASSWORD, answer_body, chapter_id, llm_service, make_folder, make_note, make_story

CONTENT_TYPE = {"Content-Type": "text/plain"}

OLD = "The door creaked. The streets glistened like wet glass under the lamplight."
NEW = "The door creaked open. The streets glistened like wet glass under the lamplight."
OLD_RUNS = [[18, 45, "gen"], [45, 54, "fix"], [54, 75, "gen"]]
NEW_RUNS = [[23, 50, "gen"], [50, 59, "fix"], [59, 80, "gen"]]
OLD_HASH = "47f57caaa4fb330e"
NEW_HASH = "39f4ef4afdf22890"

CHAPTER = Path("stories") / "example-story" / "chapters" / "first-chapter.ink"


@pytest.fixture
def stories(data_root: Path) -> Path:
    make_story(
        data_root,
        "example-story",
        title="Example Story",
        chapters={"first-chapter.ink": "Once."},
    )
    return data_root


@pytest.fixture
def notes(files_root: Path) -> Path:
    make_note(files_root, "scratch.ink", "A list.\n")
    make_folder(files_root, "research", context="Background reading.")
    return files_root


def note_id(client: TestClient) -> str:
    return client.get("/api/notes/tree").json()["files"][0]["id"]


def doc(client: TestClient, space: str, identifier: str) -> dict:
    response = client.get(f"/api/{space}/file/{identifier}/document")
    assert response.status_code == 200, response.text
    return response.json()


# --- a round trip, on both roots -------------------------------------------


def test_a_story_document_round_trips_body_and_metadata(
    logged_in: TestClient, stories: Path
) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    body = f"{OLD}\n"
    sent = {"body": body, "metadata": {OLD_HASH: OLD_RUNS}}

    assert logged_in.put(f"/api/stories/file/{chapter}/document", json=sent).status_code == 200

    answered = doc(logged_in, "stories", chapter)
    assert answered["body"] == body
    assert answered["metadata"] == {OLD_HASH: OLD_RUNS}
    assert answered["reconciled"] is None


def test_a_note_document_round_trips_body_and_metadata(
    logged_in: TestClient, notes: Path
) -> None:
    """Identical behaviour on the other root, which is what §7.4 asks for."""
    identifier = note_id(logged_in)
    body = f"{OLD}\n"
    sent = {"body": body, "metadata": {OLD_HASH: OLD_RUNS}}

    assert logged_in.put(f"/api/notes/file/{identifier}/document", json=sent).status_code == 200

    answered = doc(logged_in, "notes", identifier)
    assert answered["body"] == body
    assert answered["metadata"] == {OLD_HASH: OLD_RUNS}
    assert answered["reconciled"] is None


def test_the_provenance_lands_in_the_file_as_a_named_section(
    logged_in: TestClient, stories: Path
) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    logged_in.put(
        f"/api/stories/file/{chapter}/document",
        json={"body": f"{OLD}\n", "metadata": {OLD_HASH: OLD_RUNS}},
    )

    text = (stories / CHAPTER).read_text(encoding="utf-8")
    assert text == (
        f"===== ink:body\n{OLD}\n"
        f'===== ink:provenance\n"{OLD_HASH}": [[18, 45, "gen"], [45, 54, "fix"], [54, 75, "gen"]]\n'
    )


def test_a_header_survives_a_document_write(logged_in: TestClient, stories: Path) -> None:
    """The header is read from disk at the write, never taken from the client."""
    path = stories / CHAPTER
    path.write_text("===== ink:meta\ntitle: The Letter\n===== ink:body\nOnce.\n", encoding="utf-8")
    chapter = chapter_id(logged_in, "Example Story", "The Letter")

    logged_in.put(
        f"/api/stories/file/{chapter}/document",
        json={"body": "Twice.\n", "metadata": {provenance.paragraph_hash("Twice."): []}},
    )

    text = path.read_text(encoding="utf-8")
    assert text.startswith("===== ink:meta\ntitle: The Letter\n===== ink:body\nTwice.\n")
    assert logged_in.get(f"/api/stories/file/{chapter}").json()["name"] == "The Letter"


def test_a_section_this_build_does_not_know_survives_a_document_write(
    logged_in: TestClient, stories: Path
) -> None:
    path = stories / CHAPTER
    path.write_text("===== ink:body\nOnce.\n===== ink:outline\nkeep me\n", encoding="utf-8")
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")

    logged_in.put(f"/api/stories/file/{chapter}/document", json={"body": "Twice.\n"})

    assert "===== ink:outline\nkeep me\n" in path.read_text(encoding="utf-8")


# --- no footer, and an empty one -------------------------------------------


@pytest.mark.parametrize("space", ["stories", "notes"])
def test_a_file_with_no_footer_answers_a_null_metadata(
    logged_in: TestClient, stories: Path, notes: Path, space: str
) -> None:
    """Rather than failing. A chapter the editor has never saved has no section at all."""
    identifier = (
        chapter_id(logged_in, "Example Story", "first-chapter")
        if space == "stories"
        else note_id(logged_in)
    )
    answered = doc(logged_in, space, identifier)
    assert answered["metadata"] is None
    assert answered["reconciled"] is None
    assert answered["body"] != ""


def test_a_null_metadata_removes_the_section(logged_in: TestClient, stories: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    logged_in.put(
        f"/api/stories/file/{chapter}/document",
        json={"body": "Once.\n", "metadata": {provenance.paragraph_hash("Once."): []}},
    )
    assert "ink:provenance" in (stories / CHAPTER).read_text(encoding="utf-8")

    logged_in.put(
        f"/api/stories/file/{chapter}/document", json={"body": "Once.\n", "metadata": None}
    )

    assert "ink:provenance" not in (stories / CHAPTER).read_text(encoding="utf-8")
    assert doc(logged_in, "stories", chapter)["metadata"] is None


def test_an_empty_metadata_is_not_the_same_as_none(
    logged_in: TestClient, stories: Path
) -> None:
    """`{}` says the editor has been here and found nothing to record. `null` says it
    has never been here. Only the first writes a section."""
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    logged_in.put(f"/api/stories/file/{chapter}/document", json={"body": "", "metadata": {}})

    assert "ink:provenance" in (stories / CHAPTER).read_text(encoding="utf-8")
    assert doc(logged_in, "stories", chapter)["metadata"] == {}


# --- the cap ----------------------------------------------------------------


def test_an_oversized_body_is_refused(logged_in: TestClient, stories: Path) -> None:
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.put(
        f"/api/stories/file/{chapter}/document",
        json={"body": "x" * (MAX_FILE_BYTES + 1)},
    )

    assert response.status_code == 413
    assert (stories / CHAPTER).read_text(encoding="utf-8") == "Once."


def test_the_cap_is_on_bytes_not_characters(logged_in: TestClient, stories: Path) -> None:
    """A body of multi-byte characters short enough to count as text and long enough to
    be over the limit on disk."""
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    response = logged_in.put(
        f"/api/stories/file/{chapter}/document",
        json={"body": "é" * (MAX_FILE_BYTES // 2 + 1)},
    )

    assert response.status_code == 413


# --- recovering on read -----------------------------------------------------


@pytest.fixture
def committed(git_root: Repo, data_root: Path) -> Repo:
    """A story whose chapter holds §7.5's paragraph, committed, with its provenance."""
    make_story(data_root, "example-story", title="Example Story", chapters={})
    (data_root / CHAPTER).write_text(
        f'===== ink:body\n{OLD}\n===== ink:provenance\n"{OLD_HASH}": '
        '[[18, 45, "gen"], [45, 54, "fix"], [54, 75, "gen"]]\n',
        encoding="utf-8",
    )
    git_root.index.add([str(CHAPTER.as_posix()), "stories/example-story/story.yaml"])
    git_root.index.commit("Add the chapter")
    return git_root


def test_a_stale_hash_answers_recovered_runs(
    logged_in: TestClient, committed: Repo, data_root: Path
) -> None:
    """The writer edited the chapter in an editor of their own. The runs come back
    shifted onto the prose as it is now, and the writer sees no drift at all."""
    path = data_root / CHAPTER
    stale = path.read_text(encoding="utf-8").replace(OLD, NEW)
    path.write_text(stale, encoding="utf-8")

    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    answered = doc(logged_in, "stories", chapter)

    assert answered["metadata"] == {NEW_HASH: NEW_RUNS}
    assert answered["reconciled"]["recovered"] == [NEW_HASH]
    assert answered["reconciled"]["dropped"] == [OLD_HASH]
    assert answered["reconciled"]["revision"] == committed.head.commit.hexsha[:7]


def test_recovering_on_read_does_not_modify_the_file(
    logged_in: TestClient, committed: Repo, data_root: Path
) -> None:
    """A GET must not dirty the story repository. The file stays stale on disk until the
    next ordinary save; `ink reclassify --force` is the only thing that persists it."""
    path = data_root / CHAPTER
    stale = path.read_text(encoding="utf-8").replace(OLD, NEW)
    path.write_text(stale, encoding="utf-8")
    before = path.read_bytes()

    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    assert doc(logged_in, "stories", chapter)["metadata"] == {NEW_HASH: NEW_RUNS}

    assert path.read_bytes() == before
    assert committed.git.status("--porcelain", "--", str(CHAPTER.as_posix())).startswith(" M")


def test_a_clean_file_makes_no_git_call(
    logged_in: TestClient, committed: Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every hash matches, so there is nothing to look for. The common path must not pay
    for the rare one."""

    def refuse(*_args: object, **_kwargs: object) -> list[dict]:
        raise AssertionError("history was walked for a file with no stale paragraph")

    monkeypatch.setattr(repository, "_log_records", refuse)
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")

    answered = doc(logged_in, "stories", chapter)
    assert answered["metadata"] == {OLD_HASH: OLD_RUNS}
    assert answered["reconciled"] is None


def test_a_stale_hash_with_no_history_resets(logged_in: TestClient, stories: Path) -> None:
    """`stories` is not a git repository at all, which is most of this suite and is also
    a data root nobody has cloned. The route still answers; the paragraph resets."""
    (stories / CHAPTER).write_text(
        f'===== ink:body\n{NEW}\n===== ink:provenance\n"{OLD_HASH}": [[18, 45, "gen"]]\n',
        encoding="utf-8",
    )
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")

    answered = doc(logged_in, "stories", chapter)
    assert answered["metadata"] == {NEW_HASH: []}
    assert answered["reconciled"]["reset"] == [NEW_HASH]
    assert answered["reconciled"]["revision"] is None


def test_a_note_can_only_reset(logged_in: TestClient, notes: Path) -> None:
    """The notes root is not a repository, so there is nothing to recover from."""
    (notes / "scratch.ink").write_text(
        f'===== ink:body\n{NEW}\n===== ink:provenance\n"{OLD_HASH}": [[18, 45, "gen"]]\n',
        encoding="utf-8",
    )
    answered = doc(logged_in, "notes", note_id(logged_in))
    assert answered["metadata"] == {NEW_HASH: []}
    assert answered["reconciled"]["reset"] == [NEW_HASH]


# --- a footer that cannot be read ------------------------------------------


def test_a_provenance_section_that_is_not_yaml_costs_only_the_provenance(
    logged_in: TestClient, stories: Path
) -> None:
    """The same rule the header follows: a section nobody can read must not take the
    prose with it."""
    (stories / CHAPTER).write_text(
        "===== ink:body\nOnce.\n===== ink:provenance\n[unclosed\n", encoding="utf-8"
    )
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")

    answered = doc(logged_in, "stories", chapter)
    assert answered["body"] == "Once.\n"
    assert answered["metadata"] is None


def test_a_hash_of_nothing_but_digits_survives_a_round_trip(
    logged_in: TestClient, stories: Path
) -> None:
    """Roughly one hash in 1845 is all digits, which YAML reads back as an integer
    unless the key is quoted."""
    chapter = chapter_id(logged_in, "Example Story", "first-chapter")
    digits = "1234567890123456"
    logged_in.put(
        f"/api/stories/file/{chapter}/document",
        json={"body": "Once.\n", "metadata": {digits: [[0, 2, "gen"]]}},
    )

    assert '"1234567890123456"' in (stories / CHAPTER).read_text(encoding="utf-8")
    # The hash does not match the prose, so it is dropped rather than kept -- but as a
    # string, which is what makes it comparable at all.
    assert doc(logged_in, "stories", chapter)["reconciled"]["dropped"] == [digits]


# --- what is gone -----------------------------------------------------------


@pytest.mark.parametrize("space", ["stories", "notes"])
def test_writing_prose_alone_is_no_longer_a_route(
    logged_in: TestClient, stories: Path, notes: Path, space: str
) -> None:
    """It wrote a body and left the hashes describing text that was no longer there."""
    identifier = (
        chapter_id(logged_in, "Example Story", "first-chapter")
        if space == "stories"
        else note_id(logged_in)
    )
    response = logged_in.put(
        f"/api/{space}/file/{identifier}/contents", content="Twice.\n", headers=CONTENT_TYPE
    )
    assert response.status_code == 405


@pytest.mark.parametrize("space", ["stories", "notes"])
def test_reading_prose_alone_still_works(
    logged_in: TestClient, stories: Path, notes: Path, space: str
) -> None:
    """Reading is fine — it is the reading view, the word count and the generation path."""
    identifier = (
        chapter_id(logged_in, "Example Story", "first-chapter")
        if space == "stories"
        else note_id(logged_in)
    )
    assert logged_in.get(f"/api/{space}/file/{identifier}/contents").status_code == 200


@pytest.mark.parametrize("space", ["stories", "notes"])
def test_a_document_of_an_unknown_file_is_not_found(
    logged_in: TestClient, stories: Path, notes: Path, space: str
) -> None:
    missing = "0" * 16
    assert logged_in.get(f"/api/{space}/file/{missing}/document").status_code == 404
    assert (
        logged_in.put(
            f"/api/{space}/file/{missing}/document", json={"body": "Once."}
        ).status_code
        == 404
    )


@pytest.mark.parametrize("space", ["stories", "notes"])
def test_a_document_needs_a_token(
    client: TestClient, stories: Path, notes: Path, space: str
) -> None:
    missing = "0" * 16
    assert client.get(f"/api/{space}/file/{missing}/document").status_code == 401
    assert (
        client.put(
            f"/api/{space}/file/{missing}/document", json={"body": "Once."}
        ).status_code
        == 401
    )


# --- the background context-summary call (api#18) ---------------------------

#: Over `Settings`' own default budget (10 000 characters), so a save of this body
#: actually trims a prefix and the background call is due on the first save.
LONG_BODY = ("Jane Doe is the narrator. " * 500) + "\n\nThe end approaches now."


@pytest.fixture
def summary_client(app, settings, tmp_path: Path, user):
    """A logged-in client with a small model configured, whose provider answers
    from a transport given per test -- `title_client`'s own fixture in
    test_titles.py, for the one other route that reads `INKSPIRE_LLM_SMALL_MODEL`."""
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


def chapter_text(stories: Path) -> str:
    return (stories / CHAPTER).read_text(encoding="utf-8")


@pytest.mark.parametrize("space", ["stories", "notes"])
def test_no_small_model_schedules_nothing(
    logged_in: TestClient, stories: Path, notes: Path, space: str
) -> None:
    """The plain `settings` fixture has no small model configured; a save of a body
    well over budget must still write no section at all."""
    identifier = (
        chapter_id(logged_in, "Example Story", "first-chapter")
        if space == "stories"
        else note_id(logged_in)
    )
    logged_in.put(f"/api/{space}/file/{identifier}/document", json={"body": LONG_BODY})

    assert "ink:context_summary" not in chapter_text(stories)


def test_a_save_over_budget_writes_a_context_summary(
    summary_client: TestClient, stories: Path
) -> None:
    chapter = chapter_id(summary_client, "Example Story", "first-chapter")
    summary_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("Jane Doe is the narrator.")
    )

    response = summary_client.put(
        f"/api/stories/file/{chapter}/document", json={"body": LONG_BODY}
    )
    assert response.status_code == 200

    text = chapter_text(stories)
    assert "ink:context_summary" in text
    assert "Jane Doe is the narrator." in text


def test_a_notes_save_over_budget_writes_a_context_summary_too(
    summary_client: TestClient, notes: Path
) -> None:
    summary_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("Jane Doe is the narrator.")
    )

    response = summary_client.put(
        f"/api/notes/file/{note_id(summary_client)}/document", json={"body": LONG_BODY}
    )
    assert response.status_code == 200
    assert "ink:context_summary" in (notes / "scratch.ink").read_text(encoding="utf-8")


def test_a_save_under_budget_writes_nothing(summary_client: TestClient, stories: Path) -> None:
    chapter = chapter_id(summary_client, "Example Story", "first-chapter")
    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, text=answer_body("Should not be asked."))

    summary_client.handlers["handler"] = handler
    summary_client.put(f"/api/stories/file/{chapter}/document", json={"body": "Short."})

    assert not called
    assert "ink:context_summary" not in chapter_text(stories)


def test_a_second_save_too_soon_after_does_not_call_again(
    summary_client: TestClient, stories: Path
) -> None:
    """`is_due`'s own proportional gating (`context_summary.py`), exercised through
    the route: a second save with barely any change in what is dropped must not
    spend a second model call."""
    chapter = chapter_id(summary_client, "Example Story", "first-chapter")
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, text=answer_body("Jane Doe is the narrator."))

    summary_client.handlers["handler"] = handler
    summary_client.put(f"/api/stories/file/{chapter}/document", json={"body": LONG_BODY})
    assert len(calls) == 1

    summary_client.put(f"/api/stories/file/{chapter}/document", json={"body": LONG_BODY})
    assert len(calls) == 1  # unchanged: the second save was skipped


def test_the_summary_rate_budget_is_separate_from_generations(
    summary_client: TestClient, app, stories: Path
) -> None:
    """A writer who has been generating heavily must not starve their own
    background summaries -- `app.state.summary_limiter` is a budget of its own,
    consulted instead of `app.state.llm_limiter` by this route."""
    chapter = chapter_id(summary_client, "Example Story", "first-chapter")
    app.state.llm_limiter = RateLimiter(limit=1, interval=60)
    app.state.llm_limiter.consume(EMAIL)  # exhaust generation's own budget
    app.state.summary_limiter = RateLimiter(limit=1, interval=60)  # untouched

    summary_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("Jane Doe is the narrator.")
    )
    response = summary_client.put(
        f"/api/stories/file/{chapter}/document", json={"body": LONG_BODY}
    )

    assert response.status_code == 200
    assert "ink:context_summary" in chapter_text(stories)


def test_an_exhausted_summary_budget_writes_nothing(
    summary_client: TestClient, app, stories: Path
) -> None:
    """`limit=0` *disables* a `RateLimiter` (`throttle.py`'s own convention) --
    exhausting it instead means consuming its one allowance first, for this same
    account, the way a real writer would after one earlier save already spent it."""
    chapter = chapter_id(summary_client, "Example Story", "first-chapter")
    app.state.summary_limiter = RateLimiter(limit=1, interval=60)
    app.state.summary_limiter.consume(EMAIL)  # spend the one allowance up front

    called = []

    def handler(request: httpx.Request) -> httpx.Response:
        called.append(request)
        return httpx.Response(200, text=answer_body("Jane Doe is the narrator."))

    summary_client.handlers["handler"] = handler
    response = summary_client.put(
        f"/api/stories/file/{chapter}/document", json={"body": LONG_BODY}
    )

    assert response.status_code == 200  # the save itself still succeeds
    assert not called
    assert "ink:context_summary" not in chapter_text(stories)
