<p align="center">
  <img src="assets/logo.png" alt="InkSpire Logo" width="200"/>
</p>

# InkSpire API

## ⚠️ WARNING: Under Heavy Development ⚠️

**This project is currently under active and heavy development. It is NOT ready for general use and may contain bugs, incomplete features, or breaking changes. Use at your own risk.**

![Pytest](https://github.com/InkSpireEditor/inkspire-api/actions/workflows/test.yaml/badge.svg)
![Pylint](https://github.com/InkSpireEditor/inkspire-api/actions/workflows/lint.yaml/badge.svg)

The backend for InkSpire, a web editor for writing novels. It serves the
[InkSpire frontend](../inkspire-frontend): the stories on disk, the text a writer saves,
and the continuations a language model streams back.

---

## ✨ What it does

- **Serves the stories from a git working tree.** The filesystem decides what exists, so a
  chapter added by `git pull` or by an editor appears, and one deleted that way stops
  being served. See [Stories on disk](#stories-on-disk).
- **Streams generated text.** `POST /api/llm/generate` forwards a model's output chunk by
  chunk over server-sent events, from any of several providers.
- **Authenticates with JWTs in cookies**, rotating a refresh token, with accounts made
  from the shell rather than by registration.
- **Ships two more commands**, `lorebook` and `timeline`, which read a story's knowledge
  graph and its events from the same repository. Neither is wired to the API yet. See
  [The lorebook and the timeline](#the-lorebook-and-the-timeline).

---

## 🧠 Built with

- [FastAPI](https://fastapi.tiangolo.com/) on [Python](https://www.python.org/) 3.12 or later
- [SQLAlchemy](https://www.sqlalchemy.org/) and [Alembic](https://alembic.sqlalchemy.org/), over SQLite
- [rdflib](https://rdflib.readthedocs.io/) and [pySHACL](https://github.com/RDFLib/pySHACL) for the lorebook graph
- [Typer](https://typer.tiangolo.com/) for the `inkspire`, `lorebook` and `timeline` commands
- [Poetry](https://python-poetry.org/) for dependencies

---

## 🚀 Getting started

```bash
poetry install

# A signing secret is required and has no default.
echo "INKSPIRE_JWT_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')" >> .env.local
# Where the stories are. See "Stories on disk" below.
echo "INKSPIRE_DATA_ROOT=~/Documents/novel-data" >> .env.local

poetry run alembic upgrade head      # creates `user` and `refresh_token`
poetry run inkspire user create you@example.com
poetry run inkspire run              # http://127.0.0.1:8000
poetry run inkspire run --reload --host 0.0.0.0 --port 8001

poetry run pytest
```

`run` serves one process. The model cache, both rate limiters and the scan of the
stories are held in the serving process, so running several would multiply the
effective generation limit by the number of them. It refuses to start without a signing
secret, and warns when the stories or the provider file are not where it expects them.

The default database is `var/data_dev.db`, holding `user` and `refresh_token` and
nothing else. It is cheap to throw away and rebuild — deleting the file and running
`alembic upgrade head` again costs one `inkspire user create`.

### Accounts

There is no registration endpoint. Accounts are made and reset from the shell:

```bash
poetry run inkspire user list                                          # who exists, and their open sessions
poetry run inkspire user create alice@example.com                      # prints a generated password
poetry run inkspire user create alice@example.com -p '...' -r ROLE_ADMIN
poetry run inkspire user reset-password alice@example.com              # also revokes refresh tokens
poetry run inkspire user reset-password alice@example.com --keep-sessions
```

`list` writes its header to standard error and the rows to standard output, so the rows
pipe cleanly. A session is a refresh token that has not expired.

### Stories on disk

`INKSPIRE_DATA_ROOT` points at the story repository, a git working tree of its own:

    stories/<story-slug>/story.yaml            title, synopsis and order, written by the API
    stories/<story-slug>/chapters/<slug>.ink   a header and prose, written by the API
    stories/<story-slug>/lorebook/             read by the `lorebook` command, never by the API
    stories/<story-slug>/timeline.yaml         read by the `timeline` command, never by the API

A directory is a story if and only if it holds a `story.yaml`. The filesystem determines
what exists: a chapter dropped in by `git pull` or by an editor appears in the API, and a
chapter the manifest does not list is shown after the ones it does.

### The `.ink` file

A chapter may open with front matter — a YAML mapping between two `---` lines — and the
rest of the file is the prose:

    ---
    title: The Letter in the Study
    status: draft
    summary: |
      She finally opens it, and it is not what she was told it was.
    ---
    She had not opened it. Three years of not opening it, and the wax still held.

All three keys are optional, and so is the header. `title` is the name the chapter is
shown under, so a chapter has a name whatever its slug drops; a file with no title is
shown as its filename without the suffix. `status` is a free string — `outline`, `draft`,
`revised` and `done` are the suggested vocabulary. A key this API does not know is kept
as it is, so one added by hand survives a save.

`/contents` is the prose under that header. A read leaves the header out, and a write
keeps the header that is on disk, so the writer never sees YAML and no header reaches a
model.

Reading a header is deliberately forgiving: one that is unterminated, unparseable or not
a mapping leaves the chapter with no metadata and all of its text as prose. A malformed
first line must not take a chapter out of the tree. `inkspire ink check` is where those
problems are reported instead:

```bash
poetry run inkspire ink check                    # every .ink file in both roots
poetry run inkspire ink check stories/example-story  # or only what is named
```

It prints `path:line: level: message` and exits non-zero if any file carries an error,
so it can gate a commit. A warning alone — an unknown key — exits 0.

One story is one directory in the tree, and its chapters are that directory's files:

| Route | |
|---|---|
| `GET /api/stories/tree` | every story. `files` is empty — a chapter belongs to a story |
| `GET /api/stories/dir/{id}` | one story, the chapters in it, and which other views it has |
| `POST /api/stories/dir` | create a story, with a `name` and an optional `summary` |
| `PUT /api/stories/dir/{id}` | retitle a story, or rewrite its synopsis |
| `DELETE /api/stories/dir/{id}` | delete a story and its chapters |
| `POST /api/stories/file` | create a chapter, given a `name` and a `dir` |
| `GET /api/stories/file/{id}` | one chapter: its name, status and summary |
| `PUT /api/stories/file/{id}` | rename a chapter, or move it to another story |
| `DELETE /api/stories/file/{id}` | delete a chapter |
| `GET /api/stories/file/{id}/contents` | the chapter's prose, as `text/plain` |
| `PUT /api/stories/file/{id}/contents` | replace that prose, keeping the header |

Deleting a story is refused, with a 409, while its directory holds anything besides
`story.yaml` and `chapters/`. A lorebook and a timeline are written by hand and are not
this API's to remove, even though the commands that read them ship here.

### A story's timeline and lorebook

Read-only, and read straight from the YAML the writer authored:

| Route | |
|---|---|
| `GET /api/stories/dir/{id}/timeline` | the events laid out: coordinates, characters, arcs |
| `GET /api/stories/dir/{id}/lore/graph` | the lorebook as nodes and links |
| `GET /api/stories/dir/{id}/lore/entity/{local}` | one entity, its relations and its prose |

`GET /api/stories/dir/{id}` carries a `timeline` and a `lorebook` boolean, from whether the
files those views read are there, so a client knows which to offer without asking for either.
Most stories have one and not the other.

The timeline answers what `process()` computed — an `x1/y1/x2/y2` box per event, each
character's events as keys in date order, and the arcs. A client draws those coordinates and
lays nothing out itself, which is also what the Typst render does, so the two cannot drift.
An event's `date` is already formatted through its own `dateStyle`, so it is a display string;
the order of the `events` array is what says when things happened.

The graph answers `{nodes, links}`. A node carries its most specific class as `type`, which is
what a legend colours and filters by, along with `types`, `attrs` and `degree`. It carries no
prose: the section blocks are too bulky for a tooltip, which is why an entity can be asked for
on its own. That one answers what the Markdown sheet is rendered from — scalars, nicknames,
relations resolved to labels, and the prose sections in template order.

**Neither writes anything.** The graph is built in memory from `lorebook/data/*.yaml` on each
cache miss, so `build/lorebook.ttl` is not read and no `build/` or `export/` appears because
someone opened a view. Those stay artifacts of the shell, and what the browser shows follows
the authored YAML rather than whatever `lorebook build` last wrote. SHACL does not run here
either — `lorebook validate` is where an authoring check belongs.

Each build is held per story, against the mtimes of the files it was built from, so editing a
character in vim shows up on the next request and an unchanged lorebook is not rebuilt.

A story with no `timeline.yaml`, or no `lorebook/lorebook.yaml`, answers 404. A file that is
there and cannot be read as what it claims to be answers **422** with the reason: a timeline
missing a `positions` entry for a character combination, a date written as a bare year, an
entity typed with a class the vocabulary does not declare. Those are the writer's to fix.

### The story repository as git

`INKSPIRE_DATA_ROOT` is a git working tree, and the API drives it directly:

| Route | |
|---|---|
| `GET /api/git/status` | the branch, what has changed, and how far it is from its upstream |
| `POST /api/git/commit` | body `{message}`. Commits only what the API itself writes |
| `POST /api/git/push` | push the branch to its upstream |
| `POST /api/git/pull` | fast-forward onto the upstream |
| `GET /api/stories/file/{id}/history` | the chapter's commits, newest first |
| `GET /api/stories/file/{id}/at/{rev}` | that chapter's prose at one commit, as `text/plain` |

Saving a chapter never commits — only `/commit` does, and only onto a story's own
`story.yaml` and its `chapters/*.ink`. A hand-edited lorebook or `timeline.yaml` shows
in `/status` like anything else, marked `"committable": false`, and is never staged by
the button. `/status` never fetches, so `ahead`/`behind` are only as fresh as the last
pull, and a pull that cannot fast-forward is refused rather than merged.

Commit, push and pull each take one lock for the whole request, so a second one of
those while the first is still running is refused rather than run alongside it.

### Everything that is not a novel

Notes, lists, a draft of nothing in particular. They are not novel content, so they are
not in the story repository and are never committed: `INKSPIRE_FILES_ROOT` points at
`var/files` by default, which `.gitignore` covers, and the directory is created when
something is first written to it.

    scratch.ink                  a file at the root
    research/manifest.yaml       this folder's name and context
    research/worldbuilding.ink

A directory here is a directory and nothing more. It needs no manifest to exist, and one
is written only when there is something to keep in it — a name its slug cannot spell, or
a context. Directories do not nest: a folder holds files. Files are `.ink` files, the
same format as a chapter, so a note is shown under the title in its own header.

`/api/notes/...` answers exactly what `/api/stories/...` does — `tree`, `dir/{id}`,
`file/{id}`, `file/{id}/contents` — with two differences, both because a file here may
sit at the root:

- `POST /api/notes/file` accepts `dir: null`, and the tree's `files` map is not always
  empty.
- `PUT /api/notes/file/{id}` tells an absent `dir` from one that is explicitly `null`:
  leaving the field out leaves the file where it is, and `null` moves it to the root.

Deleting a folder takes its files and its manifest, and is refused with a 409 while it
holds anything else.

Ids carry which root they came from, so an id from one space is a 404 in the other.

Ids are `blake2b(space + path).hexdigest()[:16]`, derived from the path relative to the
root it is in — `stories` or `notes`, so the same relative path in each is two files. A
client never sends a path, so nothing it sends can point out of the repository, and every
path the API does resolve is checked to land under the root with symlinks followed. Ids
need no table and survive a restart. Renaming a chapter changes its id, because it is
then a different path; the old id answers 404 and the client refetches.

The scan is held in memory and rebuilt when the files or a manifest change. Saving a
chapter does not rebuild it: content changes no name and no path.

### Authentication

`POST /auth` takes `{"username", "password"}` and answers `{"token"}`, setting three
`SameSite=Strict` cookies: `jwt_token` (httpOnly, path `/`), `refresh_token`
(httpOnly, path `/auth`, so it is not sent with ordinary API calls) and a readable
`auth_status=1` that lets a browser client tell it has a session without reading the
token. `POST /auth/refresh` issues a new JWT and rotates the refresh token;
`POST /auth/logout` deletes it and clears all three cookies.

A request to `/api` authenticates with either the `jwt_token` cookie or an
`Authorization: Bearer` header. Tokens are signed HS256 with `INKSPIRE_JWT_SECRET`;
nothing outside this application verifies one, so there is no keypair to manage.

Errors are `{"code", "message"}` at every status.

### Text generation

Providers are configured in `config/providers.yaml`, which is not committed because it
holds API keys:

```yaml
local-ollama:
  url: http://127.0.0.1:11434
  key: null
  protocol: ollama

hosted:
  url: https://api.example.com/v1
  key: sk-...
```

The provider name becomes the prefix of every model it offers, so models are addressed
as `local-ollama/llama3`. `protocol` is `openai` by default, for any chat-completions
endpoint. Use `ollama` for an Ollama instance: its compatibility layer accepts `think`
and ignores it, so reasoning cannot be turned off through it, and sampling options
cannot be set either.

`GET /api/llm/models` lists every provider's models. A provider that cannot be reached
contributes nothing instead of failing the list. Results are held for an hour per
provider, in the serving process.

`POST /api/llm/generate` takes `{"model", "prompt"}` and answers `text/event-stream`:

| Event | Meaning |
|---|---|
| `{"delta": "..."}` | Text to append. |
| `{"error": "..."}` | The provider failed after the stream had started. |
| `[DONE]` | End of generation. |

A failure *before* any text is an ordinary status code instead — 422 for an unknown
model, 429 over the rate limit (20 generations per minute per account), 500 for an
unreachable provider — so a client only has to handle an error event once it is already
displaying text. The generated text is not written to disk: the client owns the chapter
and saves it.

Thinking is worth knowing about. A reasoning model produces its reasoning on a separate
field, which is dropped and never appended to the chapter, but it still delays the
first visible chunk by the whole reasoning pass. A model whose context window fills
with reasoning can finish without writing anything at all; that answers as an error
naming the cause rather than as an empty continuation. Set `INKSPIRE_LLM_THINK=false`
to turn it off, or raise `INKSPIRE_LLM_NUM_CTX`.

Run a generation from the shell to see all of this without a browser or an account.
The text comes from standard input and the continuation goes to standard output, so it
pipes and redirects:

```bash
poetry run inkspire llm models
poetry run inkspire llm generate -m local-ollama/llama3 < chapter.ink
poetry run inkspire llm generate -m local-ollama/llama3 --no-think < chapter.ink
poetry run inkspire llm generate -m local-ollama/llama3 --show-prompt < chapter.ink
```

It sends the same prompt the API sends, so a model that behaves badly here behaves
badly in the editor. `--show-prompt` prints what would be sent and generates nothing.

---

## 📚 The lorebook and the timeline

Two commands share this repository with the API: one dependency set, one `poetry install`,
one virtualenv. **Neither is called by the API.** They are command-line tools that read the
same story repository the API serves, and they are here so that the API can eventually call
`lorebook` as a library — the long-term goal is generating text with the graph as retrieval
context. Nothing is wired yet.

### `lorebook` — a novel's lore as an RDF graph

```
core ontology (Turtle)  +  extension.yaml  ->  Vocabulary
                                  |
YAML entity files  ->  RDF graph (rdflib)  ->  lorebook.ttl (canon)
                                  |                     |
                    generated-SHACL validation   SPARQL + Jinja2
                          (referential           -> Markdown export
                           integrity)               (11-section files)
```

The graph is the canonical source; the Markdown sheets and the HTML viewer are generated
read-views and are never hand-edited. Relations (`mentorOf`, `memberOf`, …) are real
queryable edges; prose is stored beside them as literals, so an export loses nothing.

The vocabulary is declarative and layered. `lorebook/core.ttl` defines the universal terms
in the `core:` namespace, and each lorebook's `extension.yaml` declares only its own classes
and fields in its own namespace. Adding a class or a field is a YAML edit, not a Python one,
and `rdfs:range` declarations are what the SHACL shapes are generated from, so authoring and
validation cannot drift apart.

**The root.** The directory holding one lorebook per subdirectory is resolved in this order,
first hit wins: `--root`, then `LOREBOOK_ROOT` in the environment, then `LOREBOOK_ROOT` in
`.env` or `.env.local`. No path is hardcoded — with none of the three set every command fails
rather than guessing. It is the story repository's `stories/` directory, so it is per-machine
and belongs in `.env.local`:

```bash
echo "LOREBOOK_ROOT=~/Documents/novel-data/stories" >> .env.local
```

Two shapes are accepted under the root, so one root can hold either — a standalone
`<root>/<name>/lorebook.yaml`, or a story's `<root>/<name>/lorebook/lorebook.yaml`. The
directory holding the manifest is the lorebook directory: `data/`, `build/` and `export/`
sit beside it.

```bash
poetry install -E oxigraph        # optional on-disk triplestore; the CLI does not need it

poetry run lorebook build     -l <name>   # data/*.yaml -> <lorebook>/build/lorebook.ttl
poetry run lorebook validate  -l <name>   # generated-SHACL check; exits 1 on failure
poetry run lorebook export    -l <name>   # graph -> <lorebook>/export/<id>.md
poetry run lorebook view      -l <name>   # graph -> build/lorebook-view.html, and opens it
poetry run lorebook all       -l <name>   # build, validate, export
```

`--lorebook/-l` is auto-selected only when exactly one lorebook exists. `build/` and
`export/` are generated and gitignored where they live.

`lorebook query` runs ad-hoc SPARQL with the prefixes pre-bound: `core:` and `schema:` are
shared, so a query written against them works for any lorebook, while `lore:`/`ex:` — whose
labels each manifest configures — are one extension's own.

```bash
poetry run lorebook query "SELECT ?name WHERE { ?c a core:Character ; schema:name ?name }"
poetry run lorebook query --all "SELECT ?name WHERE { ?c a core:Character ; schema:name ?name }"
poetry run lorebook query --json "SELECT ?c ?b WHERE { ?c lore:hasBloodline ?b }"
```

`--all` merges every lorebook's built graph and fails unless all of them are built. `--json`
emits SPARQL 1.1 Query Results JSON instead of TSV rows.

| Path | Role |
|---|---|
| `lorebook/core.ttl` | the shared core ontology |
| `lorebook/layout.py` | root resolution and lorebook discovery |
| `lorebook/ontology.py` | the `Vocabulary`: core, extension and manifest, assembled at runtime |
| `lorebook/authoring.py` | YAML → RDF triples |
| `lorebook/store.py` | load and save the graph as Turtle |
| `lorebook/validation.py` | pySHACL, over shapes generated from the vocabulary |
| `lorebook/export.py` | SPARQL + Jinja2 → Markdown |
| `lorebook/querying.py` | prefix union, prologue, the `--all` merge, JSON results |
| `lorebook/view.py` | graph → a self-contained interactive HTML page |
| `lorebook/vendor/` | the pinned force-graph build the page inlines |

`scripts/filter_lorebook.py` is unrelated to the pipeline: it strips a `.lorebook` export
from another tool down to text and display names.

### `timeline` — a story's events, per character

Reads a YAML timeline and renders it as Typst source. `-i` takes any path, so the input can
be a story's `timeline.yaml` in the data repository.

```bash
poetry run timeline -i <input.yaml>                 # rendered source to stdout
poetry run timeline -i <input.yaml> -o <output.typ> # or to a file
poetry run timeline -i <input.yaml> -t <template>   # another template
```

`-t` takes the name of a template shipped in `timeline/templates/` (`typst.j2` is the
default) or a path to one on disk; templates resolve through `importlib.resources`, so the
command works from any directory. `examples/timeline.yaml` is a made-up timeline that
exercises every field the renderer reads.

An event's `date` is always a full date, written any of three ways — `2019-10-03` (which
YAML reads as a date), `"2019-10-03"` and `2019-3-5` (both strings) all end up as the same
`datetime`, because events are sorted against each other and comparing a date to a datetime
raises. A bare `2019` is a number and `2019-10` is text, so neither is accepted. How much of
the date is *shown* is separate, set per event by `dateStyle`, a `strftime` format:
`"%Y-%m"` for the month, `"%Y"` for the year. Quote it — `%` cannot start a plain YAML
scalar.

As a library:

```python
from timeline import Timeline

Timeline.fromYAML("timeline.yaml").render()  # -> str
Timeline.fromDict(parsed_mapping).render()   # same, when the data is already parsed
```

`render()` lays the timeline out if the caller has not, returns the generated source and
writes nothing. `process()` is idempotent, so calling it first is optional.

---

## 🧪 Quality and testing

This project uses **AI-assisted development** for rapid implementation, with human
oversight and validation.

```bash
poetry run pytest                                    # all three suites
poetry run pytest tests/lorebook                     # one of them
poetry run pytest tests/test_files.py                # one file
poetry run pytest -k "loose or symlink"              # by name
poetry run pytest --cov inkspire_api --cov lorebook --cov timeline

poetry run pylint --rcfile=.github/workflows/pylintrc inkspire_api lorebook timeline
poetry run ty check
```

`tests/` holds the API's tests, with the other two packages' beside them in
`tests/lorebook/` and `tests/timeline/`. All three directories are packages, because all
three suites bring a `conftest.py` and a `test_cli.py` and the module names would
otherwise collide.

Tests build their own SQLite file, their own story repository and their own lorebook under
`tmp_path`, so there is nothing to set up, nothing shared between them, and nothing that
reads the authored content under `LOREBOOK_ROOT`. No provider is contacted: requests are
answered by a transport constructed in the test.

Both commands above run on every push, as the Pytest and Pylint workflows.

---

## 📜 License

This project is released under the [PolyForm Noncommercial License 1.0.0](LICENSE). Any
noncommercial purpose is permitted, which the licence spells out as including personal
study, hobby projects and use by charities, schools, public research organisations and
government bodies. It grants no licence for commercial use. The `LICENSE` file is the
terms; this paragraph is not.

It is provided *as is*, without warranty, but every effort is made to ensure code reliability and responsible use of AI-generated components.

---

## 💬 Acknowledgments

InkSpire is based on a code developped by:

- [Evann Abrial](https://www.linkedin.com/in/evann-abrial-26b446297/)
- [Lola Chalmin](https://www.linkedin.com/in/lola-chalmin-112ab9290/)
- [Roxane Rossetto](https://www.linkedin.com/in/roxane-rossetto-3b9158211/)

---

*© 2025 InkSpire. Built with care, code, and a bit of inkSpiration.*
