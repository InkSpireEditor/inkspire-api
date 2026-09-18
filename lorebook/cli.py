"""Command-line entry point tying the pipeline together.

Every command operates on one lorebook under the root (see :mod:`lorebook.layout`),
selected with ``--lorebook`` (or auto-selected when there is exactly one):

    lorebook build      data/*.yaml  -> <lorebook>/build/lorebook.ttl
    lorebook validate   SHACL check (shapes generated from the vocabulary)
    lorebook export     graph        -> <lorebook>/export/<id>.md
    lorebook query      ad-hoc SPARQL against the built graph
    lorebook view       graph        -> interactive HTML graph viewer
    lorebook all        build + validate + export

The root comes from ``--root``, else ``LOREBOOK_ROOT`` in the environment or ``.env``.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path
from typing import Optional

import typer

from . import authoring, export as export_mod, layout, store, view as view_mod
from .layout import EXPORT_REL, GRAPH_REL, VIEW_REL, RootNotConfigured
from .ontology import Vocabulary, load_vocabulary
from .querying import NotBuilt, graph_path, load_union, prologue, to_json
from .validation import validate as validate_graph

app = typer.Typer(add_completion=False, help="RDF lorebook pipeline.", no_args_is_help=True)

LorebookOption = typer.Option(
    None,
    "--lorebook",
    "-l",
    help="Lorebook under the root (auto-selected when there is only one).",
)

RootOption = typer.Option(
    None,
    "--root",
    help="Directory holding the lorebooks. Defaults to LOREBOOK_ROOT (environment or .env).",
)


def _root(override: Optional[Path]) -> Path:
    """Resolve the root, turning a missing configuration into a usage error."""
    try:
        return layout.resolve_root(override)
    except RootNotConfigured as exc:
        raise typer.BadParameter(str(exc)) from exc


def _discover(override: Optional[Path]) -> tuple[Path, dict[str, Path]]:
    """The root and every lorebook under it, as name -> lorebook directory."""
    root = _root(override)
    return root, layout.discover(root)


def _resolve(name: Optional[str], override: Optional[Path]) -> tuple[str, Path]:
    """Resolve ``--lorebook`` to a (name, directory) pair, or fail helpfully."""
    root, found = _discover(override)
    if name:
        if name not in found:
            raise typer.BadParameter(f"No lorebook {name!r} under {root}/")
        return name, found[name]
    if len(found) == 1:
        return next(iter(found.items()))
    if not found:
        raise typer.BadParameter(f"No lorebooks found under {root}/")
    names = ", ".join(found)
    raise typer.BadParameter(f"Multiple lorebooks found ({names}); pass --lorebook <name>.")


def _load(name: Optional[str], override: Optional[Path]) -> tuple[Path, Vocabulary]:
    """Resolve the lorebook directory and load its vocabulary."""
    _, directory = _resolve(name, override)
    return directory, load_vocabulary(directory)


@app.command("build")
def build(
    lorebook: Optional[str] = LorebookOption,
    root: Optional[Path] = RootOption,
) -> None:
    """Build the canonical Turtle graph from the YAML sources."""
    directory, vocab = _load(lorebook, root)
    graph = authoring.build_graph(directory, vocab)
    out = store.save_graph(graph, directory / GRAPH_REL)
    typer.echo(f"Built {len(graph)} triples -> {out}")


@app.command("validate")
def validate(
    lorebook: Optional[str] = LorebookOption,
    root: Optional[Path] = RootOption,
) -> None:
    """Validate the built graph against the generated SHACL shapes."""
    directory, vocab = _load(lorebook, root)
    graph = store.load_graph(directory / GRAPH_REL, vocab)
    conforms, report = validate_graph(graph, vocab)
    typer.echo(f"conforms: {conforms}")
    if not conforms:
        typer.echo(report)
        raise typer.Exit(code=1)


@app.command("export")
def export(
    lorebook: Optional[str] = LorebookOption,
    root: Optional[Path] = RootOption,
) -> None:
    """Regenerate the Markdown character sheets from the graph."""
    directory, vocab = _load(lorebook, root)
    graph = store.load_graph(directory / GRAPH_REL, vocab)
    for path in export_mod.export_all(graph, directory / EXPORT_REL, vocab):
        typer.echo(f"wrote {path}")


def _not_built(exc: NotBuilt) -> typer.BadParameter:
    """Turn a missing graph into a usage error naming the command that fixes it."""
    return typer.BadParameter(f"{exc.name!r} is not built yet; run: lorebook build --lorebook {exc.name}")


@app.command("query")
def query(
    sparql: str,
    lorebook: Optional[str] = LorebookOption,
    root: Optional[Path] = RootOption,
    all_lorebooks: bool = typer.Option(False, "--all", "-a", help="Query the union of every lorebook at once."),
    as_json: bool = typer.Option(False, "--json", help="Emit SPARQL 1.1 Query Results JSON instead of TSV rows."),
) -> None:
    """Run an ad-hoc SPARQL query with the relevant prefixes pre-bound.

    Against one lorebook (default) its own prefixes are bound. With ``--all`` the
    built graphs of every lorebook are merged and their prefixes unioned -- write
    such queries against the shared ``core:``/``schema:`` vocabulary for portability.
    """
    if all_lorebooks:
        if lorebook:
            raise typer.BadParameter("Pass either --lorebook or --all, not both.")
        root_dir, found = _discover(root)
        if not found:
            raise typer.BadParameter(f"No lorebooks found under {root_dir}/")
        try:
            graph, prefixes, conflicts = load_union(found)
        except NotBuilt as exc:
            raise _not_built(exc) from exc
        if conflicts:
            typer.echo(
                f"warning: prefix label(s) {conflicts} map to multiple namespaces; "
                "give those lorebooks distinct 'prefixes' in lorebook.yaml.",
                err=True,
            )
    else:
        name, directory = _resolve(lorebook, root)
        vocab = load_vocabulary(directory)
        try:
            path = graph_path(name, directory)
        except NotBuilt as exc:
            raise _not_built(exc) from exc
        graph = store.load_graph(path, vocab)
        prefixes = vocab.prefixes

    result = graph.query(prologue(prefixes) + sparql)
    if as_json:
        typer.echo(to_json(result))
        return
    for row in result:
        typer.echo("\t".join(str(v) for v in row))


@app.command("view")
def view(
    lorebook: Optional[str] = LorebookOption,
    root: Optional[Path] = RootOption,
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the generated page in a browser."),
) -> None:
    """Render the graph as a self-contained interactive HTML page.

    The page embeds the graph data inline and makes no network requests, so it
    is safe to open directly from disk and works fully offline.
    """
    directory, vocab = _load(lorebook, root)
    graph = store.load_graph(directory / GRAPH_REL, vocab)
    path = view_mod.write_view(graph, vocab, directory / VIEW_REL, title=vocab.title)
    typer.echo(f"wrote {path}")
    if open_browser:
        webbrowser.open(path.resolve().as_uri())


@app.command("all")
def all(  # noqa: A001 - a natural command name
    lorebook: Optional[str] = LorebookOption,
    root: Optional[Path] = RootOption,
) -> None:
    """Build, validate, then export in one go."""
    directory, vocab = _load(lorebook, root)
    graph = authoring.build_graph(directory, vocab)
    out = store.save_graph(graph, directory / GRAPH_REL)
    typer.echo(f"Built {len(graph)} triples -> {out}")

    conforms, report = validate_graph(graph, vocab)
    typer.echo(f"conforms: {conforms}")
    if not conforms:
        typer.echo(report)
        raise typer.Exit(code=1)

    written = export_mod.export_all(graph, directory / EXPORT_REL, vocab)
    typer.echo(f"exported {len(written)} sheets -> {directory / EXPORT_REL}")


if __name__ == "__main__":
    app()
