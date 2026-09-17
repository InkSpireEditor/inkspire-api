# -*- coding: utf-8 -*-
"""The command: stdout by default, -o to a file, and usable from any directory."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from timeline.cli import app

runner = CliRunner()


def _text(result) -> str:
    """Everything the command printed, as one line with the error box stripped."""
    try:
        raw = result.output + result.stderr
    except ValueError:  # click keeps one combined stream
        raw = result.output
    return " ".join(raw.translate(str.maketrans("", "", "│╭╮╰╯─")).split())


def test_renders_to_stdout_by_default(yaml_file: Path) -> None:
    result = runner.invoke(app, ["-i", str(yaml_file)])
    assert result.exit_code == 0, _text(result)
    assert "Jane Doe" in result.output


def test_writes_the_output_file(yaml_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.typ"
    result = runner.invoke(app, ["-i", str(yaml_file), "-o", str(out)])
    assert result.exit_code == 0, _text(result)
    assert "Jane Doe" in out.read_text(encoding="utf-8")


def test_the_progress_line_stays_off_stdout(yaml_file: Path, tmp_path: Path) -> None:
    """`timeline -i x -o y` must not put chatter where the rendered source goes."""
    out = tmp_path / "out.typ"
    result = runner.invoke(app, ["-i", str(yaml_file), "-o", str(out)])
    assert "Jane Doe" not in result.stdout


def test_runs_from_any_working_directory(yaml_file: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["-i", str(yaml_file)])
    assert result.exit_code == 0, _text(result)
    assert "Jane Doe" in result.output


def test_a_missing_input_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["-i", str(tmp_path / "absent.yaml")])
    assert result.exit_code != 0
    assert "does not exist" in _text(result)


def test_malformed_yaml_is_a_usage_error(tmp_path: Path) -> None:
    """An unquoted dateStyle is the realistic way to break the file: % starts no scalar."""
    path = tmp_path / "broken.yaml"
    path.write_text("title: Broken\nevents:\n  only:\n    dateStyle: %Y-%m\n", encoding="utf-8")
    result = runner.invoke(app, ["-i", str(path)])
    assert result.exit_code != 0
    assert "%" in _text(result)


def test_an_unknown_template_is_a_usage_error(yaml_file: Path) -> None:
    result = runner.invoke(app, ["-i", str(yaml_file), "-t", "absent.j2"])
    assert result.exit_code != 0
    assert "absent.j2" in _text(result)


def test_a_missing_position_is_a_usage_error(tmp_path: Path) -> None:
    """process() raises KeyError for an unplaced character combination; it must not traceback."""
    path = tmp_path / "unplaced.yaml"
    path.write_text(
        'title: "Unplaced"\n'
        'characters:\n  alpha:\n    name: "Jane Doe"\n    color: "blue"\n'
        'events:\n  only:\n    date: 2001-01-01\n'
        '    description: "Only"\n    characters: ["alpha"]\n',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["-i", str(path)])
    assert result.exit_code != 0
    assert "alpha" in _text(result)
