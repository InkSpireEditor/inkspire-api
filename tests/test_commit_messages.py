# -*- coding: utf-8 -*-
"""`assemble_commit_message`, `clean_commit_message`, and `POST /api/git/commit-message`.

The prompt's exact text is pinned in `tests/data/prompts.json`, as every other prompt's
is; the tests here are about what reaches it from a real index and how the route behaves.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient
from git import Repo

from tests.conftest import EMAIL, PASSWORD, answer_body, llm_service, make_story
from tests.test_llm import COMMIT_MESSAGE_PROMPTS

from inkspire_api import commit_messages, llm
from inkspire_api.repository import GitRefused
from inkspire_api.settings import get_settings
from inkspire_api.storage import Scanner
from inkspire_api.throttle import RateLimiter

CHAPTER_2 = "John Smith waited by the river until the bell rang twice.\n\nHe did not see her come in.\n"
ADDED = "Jane Doe left the forge at dawn, the guild's seal in her coat.\n"
CHAPTER_3 = "The guild met in Example Town the next morning.\n"
STORY = "stories/example"


def _manifest(root: Path, listed: list[str]) -> None:
    (root / STORY / "story.yaml").write_text(
        yaml.safe_dump(
            {"title": "Example Story", "synopsis": "", "chapters": [{"file": f} for f in listed]},
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _chapter(root: Path, filename: str, text: str) -> Path:
    path = root / STORY / "chapters" / filename
    path.write_text(text, encoding="utf-8")
    return path


def _committed_story(repo: Repo, root: Path) -> None:
    """Example Story with chapters 1, 2 and 4 committed."""
    make_story(root, "example", title="Example Story")
    _chapter(root, "01.ink", "The forge was cold.\n")
    _chapter(root, "02.ink", CHAPTER_2)
    _chapter(root, "04.ink", "She turnd the seal over in her fingres.\n")
    _manifest(root, ["01.ink", "02.ink", "04.ink"])
    repo.git.add("-A")
    repo.index.commit("Example Story")


def _example_1(repo: Repo, root: Path) -> None:
    """Chapter 2 gains a paragraph, chapter 3 is created and listed; all staged."""
    _committed_story(repo, root)
    _chapter(root, "02.ink", CHAPTER_2 + "\n" + ADDED)
    _chapter(root, "03.ink", CHAPTER_3)
    _manifest(root, ["01.ink", "02.ink", "03.ink", "04.ink"])
    repo.git.add("-A")


def _assemble(repo: Repo, root: Path, budget: int = 10_000, previous: str = ""):
    return commit_messages.assemble_commit_message(
        repo, Scanner(root), budget=budget, previous=previous
    )


# --- the exact prompt sent ------------------------------------------------------


@pytest.mark.parametrize("case", sorted(COMMIT_MESSAGE_PROMPTS))
def test_the_commit_message_prompt_is_rendered_exactly_as_recorded(case: str) -> None:
    recorded = COMMIT_MESSAGE_PROMPTS[case]
    context = commit_messages.CommitMessageContext(
        files=tuple(commit_messages.StagedFile(**file) for file in recorded["files"]),
        not_shown=tuple(recorded.get("not_shown", ())),
        previous=recorded.get("previous", ""),
    )
    assert commit_messages.render_commit_message(context) == recorded["prompt"]


# --- what the model receives ----------------------------------------------------


def test_example_1_lists_every_staged_file_labelled_with_its_diff(
    git_root: Repo, data_root: Path
) -> None:
    _example_1(git_root, data_root)
    context = _assemble(git_root, data_root)

    assert [(f.state, f.path, f.label) for f in context.files] == [
        ("modified", f"{STORY}/chapters/02.ink", 'chapter 2 of Example Story (titled "02")'),
        ("added", f"{STORY}/chapters/03.ink", 'chapter 3 of Example Story (titled "03")'),
        ("modified", f"{STORY}/story.yaml", "the manifest of Example Story"),
    ]
    assert context.files[0].diff == (
        "@@ -3 +3,3 @@\n He did not see her come in.\n+\n+" + ADDED.strip()
    )
    assert context.files[1].diff == "@@ -0,0 +1 @@\n+" + CHAPTER_3.strip()
    assert "+- file: 03.ink" in context.files[2].diff
    assert context.not_shown == ()


def test_a_chapter_title_from_its_header_is_its_label(git_root: Repo, data_root: Path) -> None:
    _committed_story(git_root, data_root)
    _chapter(data_root, "04.ink", "===== ink:meta\ntitle: The Seal\n===== ink:body\nShe turned.\n")
    git_root.git.add("-A")

    (only,) = _assemble(git_root, data_root).files
    assert only.label == 'chapter 3 of Example Story (titled "The Seal")'


def test_the_sections_are_never_sent(git_root: Repo, data_root: Path) -> None:
    """A chapter whose provenance changed with its body: only the body is compared."""
    _committed_story(git_root, data_root)
    _chapter(
        data_root,
        "04.ink",
        "===== ink:meta\ntags: [draft]\n===== ink:body\nShe turned the seal over in her fingers.\n"
        "===== ink:provenance\nhash-of-the-paragraph-0123456789: gen\n"
        "===== ink:context_summary\nA summary of the dropped text.\n",
    )
    git_root.git.add("-A")

    prompt = commit_messages.render_commit_message(_assemble(git_root, data_root))
    assert "=====" not in prompt
    assert "hash-of-the-paragraph" not in prompt
    assert "A summary of the dropped text" not in prompt
    assert "draft" not in prompt
    assert "-She turnd the seal over in her fingres.\n+She turned the seal over in her fingers." in (
        prompt
    )


def test_a_deleted_file_is_listed_by_its_path_without_a_diff(
    git_root: Repo, data_root: Path
) -> None:
    _committed_story(git_root, data_root)
    git_root.index.remove([f"{STORY}/chapters/04.ink"], working_tree=True)

    (only,) = _assemble(git_root, data_root).files
    assert (only.state, only.path, only.label, only.diff) == (
        "deleted",
        f"{STORY}/chapters/04.ink",
        f"{STORY}/chapters/04.ink",
        "",
    )


def test_a_renamed_file_shows_both_paths_and_its_body_s_diff(
    git_root: Repo, data_root: Path
) -> None:
    _committed_story(git_root, data_root)
    git_root.git.mv(f"{STORY}/chapters/02.ink", f"{STORY}/chapters/02-river.ink")
    _chapter(data_root, "02-river.ink", CHAPTER_2 + "\nThe bell rang a third time.\n")
    git_root.git.add("-A")

    renamed = next(f for f in _assemble(git_root, data_root).files if f.state == "renamed")
    assert renamed.old_path == f"{STORY}/chapters/02.ink"
    assert renamed.path == f"{STORY}/chapters/02-river.ink"
    assert "+The bell rang a third time." in renamed.diff
    prompt = commit_messages.render_commit_message(_assemble(git_root, data_root))
    assert f"- renamed {STORY}/chapters/02.ink -> {STORY}/chapters/02-river.ink" in prompt


def test_a_path_outside_the_stories_is_listed_by_its_path(
    git_root: Repo, data_root: Path
) -> None:
    (data_root / "notes.txt").write_text("a note staged by hand\n", encoding="utf-8")
    git_root.git.add("notes.txt")

    (only,) = _assemble(git_root, data_root).files
    assert (only.state, only.path, only.label, only.diff) == ("added", "notes.txt", "notes.txt", "")


def test_diffs_past_the_budget_are_named_under_not_shown(git_root: Repo, data_root: Path) -> None:
    _example_1(git_root, data_root)
    full = _assemble(git_root, data_root)
    first = len(full.files[0].diff)

    # Room for chapter 2's diff alone: chapter 3's does not fit, nor does the manifest's.
    context = _assemble(git_root, data_root, budget=first + 10)

    assert len(context.files) == 3
    assert [bool(f.diff) for f in context.files] == [True, False, False]
    assert context.not_shown == (f"{STORY}/chapters/03.ink", f"{STORY}/story.yaml")
    assert "Not shown:\n- stories/example/chapters/03.ink" in (
        commit_messages.render_commit_message(context)
    )


def test_nothing_staged_is_refused(git_root: Repo, data_root: Path) -> None:
    _committed_story(git_root, data_root)
    _chapter(data_root, "04.ink", "Edited but not staged.\n")
    with pytest.raises(GitRefused, match="Nothing is staged"):
        _assemble(git_root, data_root)


def test_the_tags_are_those_of_the_staged_chapters_in_the_index(
    git_root: Repo, data_root: Path
) -> None:
    _committed_story(git_root, data_root)
    _chapter(data_root, "04.ink", "===== ink:meta\ntags: [perso]\n===== ink:body\nShe turned.\n")
    _chapter(data_root, "02.ink", "===== ink:meta\ntags: [draft, perso]\n===== ink:body\nHe.\n")
    git_root.git.add("-A")
    # Changed after staging: the index is what the commit takes, and what is read.
    _chapter(data_root, "04.ink", "===== ink:meta\ntags: [other]\n===== ink:body\nShe.\n")

    assert set(commit_messages.staged_tags(git_root)) == {"perso", "draft"}


# --- clean_commit_message -------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("Updated: chapter 4 of Example Story", "Updated: chapter 4 of Example Story"),
        ('"Fixup: typos in chapter 2"', "Fixup: typos in chapter 2"),
        ("`Fixup: typos in chapter 2`", "Fixup: typos in chapter 2"),
        ("Commit message: Updated: chapter 4", "Updated: chapter 4"),
        ("```\nUpdated: chapter 4\n```", "Updated: chapter 4"),
        ("Updated: chapter 4.\nAdded: chapter 5", "Updated: chapter 4"),
        ("\n\n  Updated:   chapter 4  \n", "Updated: chapter 4"),
        ("Changed some things", "Changed some things"),
        ("", ""),
        ("```\n```", ""),
    ],
)
def test_clean_commit_message(raw: str, cleaned: str) -> None:
    assert commit_messages.clean_commit_message(raw) == cleaned


def test_clean_commit_message_caps_the_length() -> None:
    cleaned = commit_messages.clean_commit_message("Updated: " + "chapter " * 40)
    assert len(cleaned) <= commit_messages.MAX_PROPOSAL_LENGTH


# --- the route ------------------------------------------------------------------


@pytest.fixture
def message_client(app, settings, tmp_path: Path, user, git_root: Repo):
    """A logged-in client over a real story repository, with a small model configured,
    whose provider answers from a transport given per test and refuses `perso`."""
    handlers: dict = {}
    requests: list[httpx.Request] = []

    def dispatch(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return handlers["handler"](request)

    built = llm_service(tmp_path, dispatch, protocol="openai", refused_tags=["perso"])
    app.dependency_overrides[llm.get_service] = lambda: built
    app.dependency_overrides[get_settings] = lambda: settings.model_copy(
        update={"llm_small_model": "p/model"}
    )

    with TestClient(app) as client:
        client.post("/auth", json={"username": EMAIL, "password": PASSWORD})
        client.handlers = handlers  # type: ignore[attr-defined]
        client.requests = requests  # type: ignore[attr-defined]
        yield client


def test_proposing_a_commit_message_requires_authentication(client: TestClient) -> None:
    assert client.post("/api/git/commit-message").status_code == 401


def test_a_proposal_is_cleaned_and_returned(
    message_client, git_root: Repo, data_root: Path
) -> None:
    _example_1(git_root, data_root)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200, text=answer_body('"Added: chapter 3 of Example Story, and updated chapter 2."')
        )

    message_client.handlers["handler"] = handler
    response = message_client.post("/api/git/commit-message")

    assert response.status_code == 200
    assert response.json() == {"message": "Added: chapter 3 of Example Story, and updated chapter 2"}
    prompt = seen["messages"][0]["content"]
    assert 'chapter 3 of Example Story (titled "03")' in prompt
    assert "+" + CHAPTER_3.strip() in prompt


def test_previous_reaches_the_prompt(message_client, git_root: Repo, data_root: Path) -> None:
    _example_1(git_root, data_root)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, text=answer_body("Updated: chapter 2 of Example Story"))

    message_client.handlers["handler"] = handler
    message_client.post("/api/git/commit-message", json={"previous": "Added: chapter 3"})

    assert 'The last proposal was "Added: chapter 3"' in seen["messages"][0]["content"]


def test_previous_over_the_cap_is_refused(message_client, git_root: Repo, data_root: Path) -> None:
    _example_1(git_root, data_root)
    too_long = "x" * (commit_messages.MAX_PROPOSAL_LENGTH + 1)
    response = message_client.post("/api/git/commit-message", json={"previous": too_long})
    assert response.status_code == 400
    assert message_client.requests == []


def test_nothing_staged_is_a_409(message_client, git_root: Repo, data_root: Path) -> None:
    _committed_story(git_root, data_root)
    response = message_client.post("/api/git/commit-message")
    assert response.status_code == 409
    assert response.json()["message"] == "Nothing is staged."
    assert message_client.requests == []


def test_no_small_model_is_a_409(logged_in: TestClient, git_root: Repo, data_root: Path) -> None:
    _example_1(git_root, data_root)
    response = logged_in.post("/api/git/commit-message")
    assert response.status_code == 409
    assert "INKSPIRE_LLM_SMALL_MODEL" in response.json()["message"]


def test_a_refused_tag_on_a_staged_chapter_sends_nothing(
    message_client, git_root: Repo, data_root: Path
) -> None:
    _committed_story(git_root, data_root)
    _chapter(data_root, "04.ink", "===== ink:meta\ntags: [perso]\n===== ink:body\nShe turned.\n")
    git_root.git.add("-A")
    message_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("Updated: chapter 3")
    )

    response = message_client.post("/api/git/commit-message")

    assert response.status_code == 403
    assert message_client.requests == []


def test_an_unusable_answer_is_a_502(message_client, git_root: Repo, data_root: Path) -> None:
    _example_1(git_root, data_root)
    message_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("```\n```")
    )
    assert message_client.post("/api/git/commit-message").status_code == 502


def test_a_provider_failure_is_a_500(message_client, git_root: Repo, data_root: Path) -> None:
    _example_1(git_root, data_root)
    message_client.handlers["handler"] = lambda request: httpx.Response(500, text="boom")
    assert message_client.post("/api/git/commit-message").status_code == 500


def test_the_rate_limit_is_shared_with_generation(
    message_client, app, git_root: Repo, data_root: Path
) -> None:
    _example_1(git_root, data_root)
    app.state.llm_limiter = RateLimiter(limit=1, interval=60)
    message_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("Updated: chapter 2")
    )
    assert message_client.post("/api/git/commit-message").status_code == 200
    assert message_client.post("/api/git/commit-message").status_code == 429


def test_a_proposal_stages_and_commits_nothing(
    message_client, git_root: Repo, data_root: Path
) -> None:
    _example_1(git_root, data_root)
    head = git_root.head.commit.hexsha
    staged = git_root.git.diff("--cached", "--name-only")
    message_client.handlers["handler"] = lambda request: httpx.Response(
        200, text=answer_body("Updated: chapter 2")
    )

    message_client.post("/api/git/commit-message")

    assert git_root.head.commit.hexsha == head
    assert git_root.git.diff("--cached", "--name-only") == staged
