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

---

## 🧠 Built with

- [FastAPI](https://fastapi.tiangolo.com/) on [Python](https://www.python.org/) 3.12 or later
- [SQLAlchemy](https://www.sqlalchemy.org/) and [Alembic](https://alembic.sqlalchemy.org/), over SQLite
- [Typer](https://typer.tiangolo.com/) for the `inkspire` command
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

    stories/<story-slug>/story.yaml            title and synopsis, written by the API
    stories/<story-slug>/chapters/<slug>.ink   prose, written by the API
    stories/<story-slug>/lorebook/             read by the lorebook tool, never written here
    stories/<story-slug>/timeline.yaml         read by the timeline tool, never written here

A directory is a story if and only if it holds a `story.yaml`. The filesystem determines
what exists: a chapter dropped in by `git pull` or by an editor appears in the API, and a
chapter the manifest does not list is shown after the ones it does.

One story is one directory in the tree, and its chapters are that directory's files:

| Route | |
|---|---|
| `GET /api/tree` | every story. `files` is empty — a chapter belongs to a story |
| `GET /api/dir/{id}` | one story, and the chapters in it |
| `POST /api/dir` | create a story, with a `name` and an optional `summary` |
| `PUT /api/dir/{id}` | retitle a story, or rewrite its synopsis |
| `DELETE /api/dir/{id}` | delete a story and its chapters |
| `POST /api/file` | create a chapter, given a `name` and a `dir` |
| `GET /api/file/{id}` | one chapter's id and name |
| `PUT /api/file/{id}` | rename a chapter, or move it to another story |
| `DELETE /api/file/{id}` | delete a chapter |
| `GET /api/file/{id}/contents` | the chapter, as `text/plain` |
| `PUT /api/file/{id}/contents` | replace the chapter with the request body |

Deleting a story is refused, with a 409, while its directory holds anything besides
`story.yaml` and `chapters/`. A lorebook and a timeline are written by hand and are not
this API's to remove.

**Known limitation.** A directory here is a story and a file is a chapter, so there is
nowhere to put a file that belongs to no story: `POST /api/file` with `dir: null`
answers 422, `GET /api/tree` always returns an empty `files` map, and every directory
created is a story. The umbrella repository's `ARCHITECTURE.md` records what has to be
decided to lift that.

Ids are `blake2b(path).hexdigest()[:16]`, derived from the repository-relative path. A
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

## 🧪 Quality and testing

This project uses **AI-assisted development** for rapid implementation, with human
oversight and validation.

```bash
poetry run pytest                                    # the suite
poetry run pytest --cov inkspire_api                 # with coverage
poetry run pytest tests/test_files.py                # one file
poetry run pytest -k "loose or symlink"              # by name

poetry run pylint --rcfile=.github/workflows/pylintrc inkspire_api
poetry run ty check
```

Tests build their own SQLite file and their own story repository under `tmp_path`, so
there is nothing to set up and nothing shared between them. No provider is contacted:
requests are answered by a transport constructed in the test.

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
