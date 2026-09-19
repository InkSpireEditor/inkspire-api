"""Root resolution and discovery: --root, LOREBOOK_ROOT, .env, and both directory shapes.

Every test that resolves a root from the environment chdirs into ``tmp_path`` first, so
the repository's own ``.env`` cannot supply one behind the test's back.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from lorebook.layout import RootNotConfigured, discover, manifest_dir, resolve_root


def test_manifest_in_the_entry_itself(tmp_path: Path, make_lorebook: Callable[..., Path]) -> None:
    make_lorebook(tmp_path / "standalone", "standalone")
    assert manifest_dir(tmp_path / "standalone") == tmp_path / "standalone"


def test_manifest_one_level_down(tmp_path: Path, make_lorebook: Callable[..., Path]) -> None:
    """A novel-data story keeps its lorebook in ``<story>/lorebook/``."""
    story = tmp_path / "a_story"
    make_lorebook(story / "lorebook", "a_story")
    (story / "chapters").mkdir()
    (story / "story.yaml").write_text("title: A Story\nchapters: []\n", encoding="utf-8")
    assert manifest_dir(story) == story / "lorebook"


def test_a_directory_without_a_manifest_is_not_a_lorebook(tmp_path: Path) -> None:
    (tmp_path / "not_a_lorebook" / "data").mkdir(parents=True)
    assert manifest_dir(tmp_path / "not_a_lorebook") is None


def test_discover_keys_by_entry_name_not_manifest_directory(
    tmp_path: Path, make_lorebook: Callable[..., Path]
) -> None:
    make_lorebook(tmp_path / "flat", "flat")
    make_lorebook(tmp_path / "nested" / "lorebook", "nested")
    (tmp_path / "empty").mkdir()
    (tmp_path / "loose.txt").write_text("", encoding="utf-8")

    found = discover(tmp_path)

    # 'nested' keeps its own name even though its manifest sits in 'lorebook/'.
    assert list(found) == ["flat", "nested"]
    assert found["flat"] == tmp_path / "flat"
    assert found["nested"] == tmp_path / "nested" / "lorebook"


def test_discover_on_a_missing_root_is_empty(tmp_path: Path) -> None:
    assert discover(tmp_path / "nowhere") == {}


def test_override_wins_over_the_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOREBOOK_ROOT", str(tmp_path / "from_env"))
    assert resolve_root(tmp_path / "from_flag") == tmp_path / "from_flag"


def test_environment_supplies_the_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOREBOOK_ROOT", str(tmp_path / "stories"))
    assert resolve_root() == tmp_path / "stories"


def test_a_tilde_is_expanded(tmp_path: Path, monkeypatch) -> None:
    """A ``.env`` goes through no shell, so ``~`` has to be expanded here."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("LOREBOOK_ROOT", "~/stories")
    assert resolve_root() == Path.home() / "stories"


def test_dotenv_supplies_the_root(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOREBOOK_ROOT", raising=False)
    (tmp_path / ".env").write_text("LOREBOOK_ROOT=~/from_dotenv\n", encoding="utf-8")
    assert resolve_root() == Path.home() / "from_dotenv"


def test_an_unset_root_is_an_error_not_a_default(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOREBOOK_ROOT", raising=False)
    with pytest.raises(RootNotConfigured):
        resolve_root()
