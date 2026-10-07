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
- **Streams generated text.** `POST /api/stories|notes/file/{id}/generate` forwards a
  model's output chunk by chunk over server-sent events, from any of several providers —
  continuing past the end of the chapter, or filling in a caret reported mid-file.
- **Records who wrote each character.** A chapter keeps, beside its prose, which stretches a
  model wrote and which of those the writer has since corrected — and recovers that record
  from git when the file is edited outside the editor. See [The `.ink` file](#the-ink-file).
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

A file is a sequence of named sections. Each opens with a fence line and runs to the next
fence or to the end of the file:

    ===== ink:meta
    title: The Letter in the Study
    status: draft
    summary: |
      She finally opens it, and it is not what she was told it was.
    ===== ink:body
    She had not opened it. Three years of not opening it, and the wax still held.
    ===== ink:provenance
    "47f57caaa4fb330e": [[18, 45, "gen"], [45, 54, "fix"]]

**A file with no fence line anywhere is all prose**, which is what a writer gets by making
a file and typing in it, and what keeps these readable to anything that reads text.

`ink:meta` is a YAML mapping and all of its keys are optional, as is the section itself.
`title` is the name the chapter is shown under, so a chapter has a name whatever its slug
drops; a file with no title is shown as its filename without the suffix. `status` is a free
string — `outline`, `draft`, `revised` and `done` are the suggested vocabulary. A key this
API does not know is kept as it is, so one added by hand survives a save. It has to come
first: listing a tree reads only the top of each file, so a header further down would not be
found.

`ink:body` is the prose, and `/contents` is exactly it — nothing to strip, no markup in it.

`ink:provenance` records who wrote each character: one entry per paragraph, keyed by a hash
of that paragraph's own text, whose value is the stretches that are not the writer's own as
`[start, end, kind]` with offsets relative to the paragraph. `gen` is a model's text, `fix`
is a model's text the writer has since corrected, and the writer's own is the default and so
is never stored. Keying by content rather than by position means editing one paragraph leaves
every other paragraph's record untouched.

**`docs/ink-format.md` and `docs/provenance.md`** are these two in full — the grammar, what
reading forgives and what `ink check` reports, the paragraph-split contract the frontend has
to match byte for byte, and how a record left stale by a hand edit is recovered.

`---` means nothing here, so it is free to be a Markdown horizontal rule in prose.

**A section this build does not know is preserved exactly**, so a newer build's metadata
survives a round trip through an older one.

Reading is deliberately forgiving. A header that is not YAML costs the metadata and nothing
else — the fences say where the prose starts, so a broken header cannot take the prose with
it — and a file nobody can make sense of keeps all of its text. A malformed line must never
take a chapter out of the tree. `inkspire ink check` is where those problems are reported
instead:

```bash
poetry run inkspire ink check                        # every .ink file in both roots
poetry run inkspire ink check stories/example-story  # or only what is named
```

It prints `path:line: level: message` and exits non-zero if any file carries an error, so it
can gate a commit. A warning alone — a key or a section it has no meaning for — exits 0.
Errors are prose above the first section, a section opened twice, a `meta` section that is
not first or is not a YAML mapping, and a known key whose value is not text.

### Provenance a hand edit left behind

Editing a chapter outside the editor changes a paragraph's text, so it no longer hashes to
the key its record is stored under — and those offsets now describe prose that is gone.
**Recomputing the hash is not the fix**: it would record the new text while keeping offsets
measured against the old, turning something detectable into a silent wrong answer.

So the paragraph's own earlier text is looked for in the file's git history, diffed forward,
and the record replayed over the result: characters that survived keep what they were,
inserted ones become the writer's, deleted ones contribute nothing. What history cannot
explain is dropped rather than guessed at, and no model is ever asked which passages read as
machine-written — that would fabricate a record rather than recover one.

`GET /document` does this before answering, so the writer normally never sees a drift, and it
**writes nothing** doing so: recovering on a read must not dirty the story repository. To put
it right on disk:

```bash
poetry run inkspire ink reclassify                   # report; writes nothing
poetry run inkspire ink reclassify -f                # apply
poetry run inkspire ink reclassify -n chapters/02.ink  # a dry run, said out loud
```

    chapters/02.ink
      para 1  080e…6029  ok
      para 2  39f4…2890  STALE  -> recoverable from 07e9299, was 47f5…330e (3 runs)
      para 3  7f39…2f97  ok
    1 file checked, 1 stale, 1 recoverable, 0 would reset. Nothing written; pass --force to apply.

A dry run exits non-zero if it found anything stale, so it too can gate a commit, and a clean
file prints nothing at all. Unlike the route this walks the whole history rather than
stopping at a ceiling: it is run by hand, after a miss, and is worth the time. Notes have no
history — their root is not a repository — so there a stale paragraph can only be dropped.

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
| `GET /api/stories/file/{id}/document` | the prose **and** its provenance, reconciled |
| `PUT /api/stories/file/{id}/document` | replace both, in one write |

Deleting a story is refused, with a 409, while its directory holds anything besides
`story.yaml` and `chapters/`. A lorebook and a timeline are written by hand and are not
this API's to remove, even though the commands that read them ship here — the 409 body
names them in `holds`. `?force=true` deletes the directory whole regardless.

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
`file/{id}`, `file/{id}/contents` and `file/{id}/document` — with three differences. Two are
because a file here may sit at the root:

- `POST /api/notes/file` accepts `dir: null`, and the tree's `files` map is not always
  empty.
- `PUT /api/notes/file/{id}` tells an absent `dir` from one that is explicitly `null`:
  leaving the field out leaves the file where it is, and `null` moves it to the root.

The third is that this root is not a git repository, so there is no history here to recover a
stale provenance record from — a paragraph edited outside the editor can only have its record
dropped.

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

Errors are at least `{"code", "message"}` at every status. `DELETE /api/stories/dir/{id}`
also carries `holds`, the entries in the way, when it answers 409; `?force=true` deletes
the directory regardless of what is in it.

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

`GET /api/llm/models` lists every provider's models, each carrying its `protocol` so a
client can tell which models can honour `think` at all. A provider that cannot be
reached contributes nothing instead of failing the list. Results are held for an hour
per provider, in the serving process. `GET /api/llm/defaults` answers the server's own
generation settings — see below — so a client has something to show before it overrides
anything.

`POST /api/stories/file/{id}/generate` and its `/api/notes` counterpart take
`{"model", ...}` and answer `text/event-stream`. The request carries no text: the server
reads `id`'s file fresh from disk, so a pending edit has to be saved first — the editor
does this itself before every generation. Optional fields:

| Field | Overrides | Omitted means |
|---|---|---|
| `think` | `INKSPIRE_LLM_THINK` | the setting applies; reaches the provider only on the `ollama` protocol |
| `cursor_para`, `cursor_offset` | — | continue at the end of the file (both are required together, or not at all) |
| `temperature` | `INKSPIRE_LLM_TEMPERATURE` | the setting applies |
| `prompt_budget` | `INKSPIRE_LLM_PROMPT_BUDGET` | the setting applies |
| `prefix_share` | `INKSPIRE_LLM_PREFIX_SHARE` | the setting applies |
| `num_ctx` | `INKSPIRE_LLM_NUM_CTX` | the setting applies (unset by default — the model's own default) |

A caret splits the file into a prefix and a suffix and asks the model to write what
belongs between them; with no caret, or one at the very end of the file, the whole
file is the prefix and the model continues past it instead — the common case, and
identical to what this endpoint always did before the caret existed. Either way, each
side is trimmed to its share of the prompt budget at a paragraph boundary, silently.
See `docs/prompt.md` for the full assembly, the trim, and why `num_ctx` has to sit above
the budget.

| Event | Meaning |
|---|---|
| `{"delta": "..."}` | Text to append. |
| `{"error": "..."}` | The provider failed after the stream had started. |
| `[DONE]` | End of generation. |

A failure *before* any text is an ordinary status code instead — 422 for an unknown
model, an out-of-range caret, or a caret with only one of its two fields given; 429 over
the rate limit (20 generations per minute per account); 500 for an unreachable provider
— so a client only has to handle an error event once it is already displaying text. The
generated text is not written to disk: the client owns the chapter and saves it.

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
poetry run inkspire llm generate -m local-ollama/llama3 --show-prompt --cursor 500 < chapter.ink
```

It sends the same prompt the API sends, so a model that behaves badly here behaves
badly in the editor. `--show-prompt` prints what would be sent and generates nothing;
`--cursor` is a flat character offset into standard input, converted to a paragraph and
an offset the same way the route resolves one reported directly.

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

## 📖 Docs

Multiple documents under `docs/`, each on one subject:

| | |
|---|---|
| [`filesystem.md`](docs/filesystem.md) | what is actually on disk: the two roots, the manifests, the ids |
| [`entries.md`](docs/entries.md) | the classes a scan turns that into — `File`, `Folder` and what each root adds |
| [`ink-format.md`](docs/ink-format.md) | the `.ink` file: its sections, its grammar, and the rules reading enforces |
| [`provenance.md`](docs/provenance.md) | who wrote each character, and how that survives a hand edit |

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
