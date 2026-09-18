# -*- coding: utf-8 -*-
"""Command-line entry point: a YAML timeline in, rendered source out.

    timeline -i <input.yaml> [-o <output>] [-t <template>]

With no ``-o`` the result goes to stdout, so the command composes with a pipe. The
template is resolved inside the package, so the command runs from any directory.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
import yaml

from .model import DEFAULT_TEMPLATE, Timeline

app = typer.Typer(add_completion=False, help="Render a story timeline from a YAML file.")


# One command, so Typer collapses it into the app and `no_args_is_help` belongs here
# rather than on the Typer above, where it would apply to a group that does not exist.
@app.command("render", no_args_is_help=True)
def render(
    input: Path = typer.Option(  # noqa: A002 - the flag has always been -i/--input
        ...,
        "--input",
        "-i",
        exists=True,
        dir_okay=False,
        readable=True,
        help="YAML timeline to read.",
    ),
    output: Optional[Path] = typer.Option(
        None,
        "--output",
        "-o",
        dir_okay=False,
        help="Where to write the rendered source. Defaults to stdout.",
    ),
    template: str = typer.Option(
        DEFAULT_TEMPLATE,
        "--template",
        "-t",
        help="Template shipped in timeline/templates, or a path to one on disk.",
    ),
) -> None:
    """Render a timeline and write it to a file or to stdout."""
    try:
        timeline = Timeline.fromYAML(input)
        rendered = timeline.render(template)
    except KeyError as exc:
        # process() raises KeyError for a character combination with no positions entry,
        # and fromDict() for a missing required field. Neither deserves a traceback.
        raise typer.BadParameter(str(exc)) from exc
    except (FileNotFoundError, TypeError, yaml.YAMLError) as exc:
        # An unreadable template, an unusable date, or malformed YAML.
        raise typer.BadParameter(str(exc)) from exc

    if output is None:
        typer.echo(rendered)
    else:
        output.write_text(rendered, encoding="utf-8")
        typer.echo(f"wrote {output}", err=True)


if __name__ == "__main__":
    app()
