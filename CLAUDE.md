# CLAUDE.md

Guidance for Claude Code (claude.ai/code) working in this repository. The parent directory's
`CLAUDE.md` covers InkSpire as a whole and the API itself; this file covers the two packages
that share the repository with it.

## Three packages, one environment

`inkspire_api/` is the FastAPI service. `lorebook/` and `timeline/` are command-line tools
that read the same story repository the API serves. One `pyproject.toml`, one `poetry.lock`,
one virtualenv, three entry points:

```bash
poetry run inkspire ...      # the API and its administration
poetry run lorebook ...      # the RDF lorebook pipeline
poetry run timeline ...      # events per character, rendered as Typst
```

**Nothing in `inkspire_api/` imports `lorebook` or `timeline`, and neither imports the
other.** They share this repository so the API can eventually call `lorebook` as a library —
the same three calls its CLI makes (`load_vocabulary`, `store.load_graph`,
`graph.query(prologue(...) + sparql)`). That is not wired, and `ARCHITECTURE.md` §8 says why
it needs a design pass first: `core.ttl` has no temporal or narrative vocabulary, so linking
timeline events to chapters to lorebook entities is unsolved.

Tests live in `tests/` (the API), `tests/lorebook/` and `tests/timeline/`. All three
directories are packages — each suite has a `conftest.py` and a `test_cli.py`, so without
`__init__.py` the module names collide. The API's tests import helpers as
`from tests.conftest import ...` for the same reason.

`LOREBOOK_ROOT` is read from `.env`/`.env.local` alongside the `INKSPIRE_`-prefixed keys;
each settings class ignores the other's. It is the story repository's `stories/` directory,
which is per-machine, so the real value belongs in `.env.local`.

---

## lorebook

```bash
poetry install                      # add -E oxigraph for the optional on-disk store

poetry run lorebook build     -l <name>   # data/*.yaml -> <lorebook>/build/lorebook.ttl
poetry run lorebook validate  -l <name>   # generated-SHACL check; exits 1 on failure
poetry run lorebook export    -l <name>   # graph -> <lorebook>/export/<id>.md
poetry run lorebook view      -l <name>   # graph -> build/lorebook-view.html (+ opens browser)
poetry run lorebook all       -l <name>   # build + validate + export
poetry run lorebook query [--all] [--json] "SELECT ..."
```

`--lorebook/-l` is auto-selected only when exactly one lorebook exists; with several it is
required. `query --all` merges every lorebook's graph and fails unless **all** of them are
built first. Both `build/` and `export/` are gitignored generated artifacts.

### The root

`lorebook/layout.py` resolves the directory holding the lorebooks: `--root`, else
`LOREBOOK_ROOT` in the environment, else `LOREBOOK_ROOT` in `.env`/`.env.local`. **No path is
hardcoded in Python** — with none of the three set, every command fails instead of falling
back.

`manifest_dir()` accepts two shapes, so one root can hold both a standalone
`<root>/<name>/lorebook.yaml` and a story's `<root>/<name>/lorebook/lorebook.yaml`. The
directory holding the manifest is the lorebook directory (`data/`, `build/`, `export/` sit
beside it); `<name>` is always the directory under the root, never the manifest's own
directory. `discover()` returns `{name: lorebook directory}` and is what both `--lorebook`
and `--all` use.

Because `Settings` reads `.env` relative to the working directory, tests must pass `--root`
explicitly (or `monkeypatch.chdir(tmp_path)`) so the repository's `.env`/`.env.local` cannot
leak into them.

### Architecture

Pipeline: `YAML authoring → RDF graph (rdflib) → generated SHACL → Markdown/HTML views`.
**The graph is canonical; Markdown and HTML are regenerated read-views — never hand-edit
`export/*.md`.** Relations are real graph edges; narrative prose is stored as literal
properties on the same subject so the export stays full-fidelity.

#### The vocabulary is data, not code

`lorebook/ontology.py` assembles a `Vocabulary` at runtime from two declarative sources:

- `lorebook/core.ttl` — the shared universal terms (`Character`, `Location`, `mentorOf`, …)
  in the `core:` namespace.
- `<lorebook>/extension.yaml` — that lorebook's *own* classes/properties, minted in
  its own `onto` namespace declared in `lorebook.yaml`.

Adding a class or field to a lorebook means editing YAML/Turtle, **not Python**. The loader's
conventions are what make this work:

- `owl:Class` → a node type; `owl:ObjectProperty` → an edge field; `owl:DatatypeProperty` → a
  literal field.
- **The authoring field name is the term's local name.** `core:mentorOf` is authored as
  `mentorOf:` in a character YAML file. Renaming a term renames the authoring field.
- `rdfs:domain`/`rdfs:range` are what `validation.py` *generates* the SHACL shapes from, so
  authoring and validation cannot drift. Never hand-write shapes — declare a range instead.
- An extension listing a property that already exists in core (e.g. `memberOf`) **extends** it:
  the extra ranges union with the core ones rather than replacing them.
- `rdfs:subClassOf` is real, and it works by **materialisation, not reasoning**: `authoring.py`
  stamps each authored type together with its ancestors (`Vocabulary.class_closure`), so
  `type: SecondaryCharacter` also emits `a core:Character` and `a core:Entity`. Nothing
  downstream does subclass inference — the exporter, the generated shapes and SPARQL all match
  `rdf:type` exactly — so the closure is what makes a subclass usable wherever its parent is.
  Consequence: nearly every node is also a `core:Entity`, which `view.py` hides unless it is a
  node's only type. The viewer colours and filters by a node's **most specific** class (deepest
  in the hierarchy, ties alphabetical) — picking the shallowest would collapse every subclass
  into its parent and make it unfilterable in the legend.

Every module takes `(graph, vocab)` and resolves terms through `Vocabulary` — do not hardcode
namespaces, prefixes or field names in the engine. Prefix labels (`lore:`/`ex:` by default) are
per-lorebook configurable via `lorebook.yaml`'s `prefixes:`; the test fixture deliberately uses
non-default labels (`tb`/`tbx`) to catch hardcoding. `core`/`schema`/`rdfs` are reserved.

`rdflib` types most graph accessors as `Node`, so a value the code knows is a URI is
converted at the boundary — `URIRef(str(node))`, or an `isinstance` guard as in
`class_closure`. The `Prefixes` alias in `ontology.py` exists because `schema:` and `rdfs:`
are `DefinedNamespace` classes rather than `Namespace` instances.

#### Authoring surface (`data/`)

- `entities.yaml` — flat `id: type` (or `id: {type, name}`) stubs for every non-character node.
  These typed stubs exist so SHACL can verify that a relation points at a real node of the
  declared class; a typo'd reference fails validation instead of silently creating a dangling edge.
- `characters/*.yaml`, `locations/*.yaml` — full records: scalars, relation lists, and a
  `sections:` prose map. `authoring.py` only reads these two subdirectories.
- `export.py` skips any node with no prose sections, so stubs never produce a sheet.

#### Export templates are shared but field-aware

`templates/character.md.j2` and `location.md.j2` are shared by all lorebooks and guard each
field with `{% if %}`, including extension-specific ones (`hasBloodline`, `troupeRole`, …).
A new extension property therefore appears in the graph and validation automatically, but shows
up in a sheet's Basic Information block only once a line is added to the template. The prose
section headings and their order live in `SECTION_ORDER` / `LOCATION_SECTION_ORDER` in
`export.py`, keyed to the `sections:` keys authors write.

#### Viewer constraints (`view.py`)

The HTML viewer is intentionally hardened and must stay that way: graph data is embedded inline
(no network requests, no CORS proxy), text reaches the DOM only escaped or via canvas `fillText`
(never `innerHTML`), the embedded JSON is delimiter-escaped and `JSON.parse`d, and force-graph is
a pinned vendored UMD build under `lorebook/vendor/` — do not swap it for a CDN reference.

#### Tests

`tests/lorebook/` runs against `tests/lorebook/fixtures/testbook`, a self-contained fake
lorebook, or against one the `make_lorebook` fixture writes into `tmp_path` — never against
real content under the configured root, so editing or deleting an authored lorebook must not
break the suite. Keep it that way when adding tests.

---

## timeline

```bash
poetry run timeline -i <input.yaml>                 # rendered source to stdout
poetry run timeline -i <input.yaml> -o <output.typ> # or to a file
poetry run timeline -i <input.yaml> -t <template>   # typst.j2 by default

poetry run pytest tests/timeline
```

`timeline/model.py` holds `Character`, `Event`, `Arc` and `Timeline`. `Timeline.fromYAML` and
`fromDict` parse, `process()` lays events out, `render()` fills a Jinja template.
`process()` is idempotent by a `_processed` guard: a second pass would fail, because the
first replaces each arc's event *key* with the event itself.

`_arcs` and `_positions` are keyed maps, so their empty default is a dict. Every event's
`date` is a full date and is parsed to a `datetime`, because events are compared against each
other; `dateStyle` is a separate `strftime` format controlling how much of it a template
shows. Templates resolve through `importlib.resources`, so `-t typst.j2` works from any
directory and a path is accepted too.

This module predates the rest and uses camelCase attributes and untyped signatures. Match the
surrounding style when editing it rather than converting it piecemeal.

`examples/timeline.yaml` exercises every field the renderer reads, with placeholder names. The
authored timelines live in the story repository, not here.
