"""The CLI end to end against a temporary root: --root, --json, --all, and the errors.

These tests always pass ``--root`` explicitly, so they never read the repository's
``.env`` or the authored content it points at.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from lorebook.cli import app

runner = CliRunner()


def _text(result) -> str:
    """Everything the command printed, as one line.

    Typer draws errors in a box and wraps them to the terminal width, so the border
    characters are dropped and whitespace collapsed before matching on a message.
    """
    try:
        raw = result.output + result.stderr
    except ValueError:  # click keeps one combined stream
        raw = result.output
    return " ".join(raw.translate(str.maketrans("", "", "│╭╮╰╯─")).split())


@pytest.fixture()
def root(tmp_path: Path, make_lorebook: Callable[..., Path]) -> Path:
    """A root holding one lorebook of each shape: ``flat/`` and ``nested/lorebook/``."""
    make_lorebook(tmp_path / "flat", "flat", onto_prefix="alpha", entity_prefix="ax")
    make_lorebook(tmp_path / "nested" / "lorebook", "nested",
                  onto_prefix="beta", entity_prefix="bx")
    return tmp_path


def _build(root: Path, name: str) -> None:
    result = runner.invoke(app, ["build", "--root", str(root), "--lorebook", name])
    assert result.exit_code == 0, _text(result)


def test_build_writes_beside_the_manifest_not_above_it(root: Path) -> None:
    _build(root, "nested")
    assert (root / "nested" / "lorebook" / "build" / "lorebook.ttl").exists()
    assert not (root / "nested" / "build").exists()


def test_build_finds_a_flat_lorebook_too(root: Path) -> None:
    _build(root, "flat")
    assert (root / "flat" / "build" / "lorebook.ttl").exists()


def test_query_one_lorebook(root: Path) -> None:
    _build(root, "nested")
    result = runner.invoke(
        app,
        ["query", "--root", str(root), "--lorebook", "nested",
         "SELECT ?n WHERE { ?c a core:Character ; schema:name ?n }"],
    )
    assert result.exit_code == 0, _text(result)
    assert "Hero nested" in result.output


def test_query_json_emits_sparql_results_json(root: Path) -> None:
    _build(root, "nested")
    result = runner.invoke(
        app,
        ["query", "--root", str(root), "--lorebook", "nested", "--json",
         "SELECT ?n WHERE { ?c a core:Character ; schema:name ?n }"],
    )
    assert result.exit_code == 0, _text(result)
    payload = json.loads(result.output)
    assert payload["head"]["vars"] == ["n"]
    assert [b["n"]["value"] for b in payload["results"]["bindings"]] == ["Hero nested"]


def test_query_all_spans_both_shapes(root: Path) -> None:
    _build(root, "flat")
    _build(root, "nested")
    result = runner.invoke(
        app,
        ["query", "--root", str(root), "--all",
         "SELECT ?n WHERE { ?c a core:Character ; schema:name ?n }"],
    )
    assert result.exit_code == 0, _text(result)
    assert {"Hero flat", "Hero nested"} <= set(result.output.split("\n"))


def test_query_all_refuses_a_lorebook_name(root: Path) -> None:
    result = runner.invoke(app, ["query", "--root", str(root), "--all", "-l", "flat", "SELECT * {}"])
    assert result.exit_code != 0
    assert "not both" in _text(result)


def test_query_all_names_the_unbuilt_lorebook(root: Path) -> None:
    _build(root, "flat")  # 'nested' is deliberately left unbuilt
    result = runner.invoke(app, ["query", "--root", str(root), "--all", "SELECT * {}"])
    assert result.exit_code != 0
    assert "lorebook build --lorebook nested" in _text(result)


def test_unknown_lorebook_names_the_root(root: Path) -> None:
    result = runner.invoke(app, ["build", "--root", str(root), "--lorebook", "absent"])
    assert result.exit_code != 0
    assert "absent" in _text(result) and str(root) in _text(result)


def test_a_single_lorebook_is_auto_selected(tmp_path: Path,
                                            make_lorebook: Callable[..., Path]) -> None:
    make_lorebook(tmp_path / "only" / "lorebook", "only")
    result = runner.invoke(app, ["build", "--root", str(tmp_path)])
    assert result.exit_code == 0, _text(result)
    assert (tmp_path / "only" / "lorebook" / "build" / "lorebook.ttl").exists()


def test_several_lorebooks_require_a_name(root: Path) -> None:
    result = runner.invoke(app, ["build", "--root", str(root)])
    assert result.exit_code != 0
    assert "flat, nested" in _text(result)


def test_an_empty_root_says_so(tmp_path: Path) -> None:
    result = runner.invoke(app, ["build", "--root", str(tmp_path)])
    assert result.exit_code != 0
    assert "No lorebooks found" in _text(result)


def test_without_a_root_anywhere_the_command_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("LOREBOOK_ROOT", raising=False)
    result = runner.invoke(app, ["build"])
    assert result.exit_code != 0
    assert "No lorebook root configured" in _text(result)
