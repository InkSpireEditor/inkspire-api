<p align="center">
  <img src="assets/logo.png" alt="InkSpire Logo" width="200"/>
</p>

# InkSpire API

## ⚠️ WARNING: Under Heavy Development ⚠️

**This project is currently under active and heavy development. It is NOT ready for general use and may contain bugs, incomplete features, or breaking changes. Use at your own risk.**

![CI](https://github.com/InkSpireEditor/inkspire-api/actions/workflows/ci.yml/badge.svg?branch=main)

This is the backend API for InkSpire, a modern web-based text editor. It provides all the necessary services for the [InkSpire Frontend](../inkspire-frontend) to function.

---

## ✨ Features

- **RESTful API**: Provides a complete set of endpoints for file and directory management.
- **JWT Authentication**: Secures the API using JSON Web Tokens for stateless authentication.

---

## 🧠 Technology Stack

- [Symfony](https://symfony.com/) — A set of reusable PHP components and a PHP framework to build web applications.
- [PHP](https://www.php.net/) 8.2+

---

## 🚀 Getting Started

### Prerequisites

- [PHP](https://www.php.net/) 8.2 or later
- [Composer](https://getcomposer.org/)
- [Symfony CLI](https://symfony.com/download)

### Installation

1.  **Clone the repository:**
    ```bash
    git clone <repository-url>
    cd inkspire-api
    ```

2.  **Install dependencies:**
    ```bash
    composer install
    ```

3.  **Set up environment variables:**
    Create a `.env` file and configure your database connection and other variables. You will need to generate the JWT keys.
    ```bash
    php bin/console lexik:jwt:generate-keypair
    ```
    This will generate `config/jwt/private.pem` and `config/jwt/public.pem` and update your `.env` file.

5.  **Run database migrations:**
    ```bash
    php bin/console doctrine:database:create --env=dev
    php bin/console doctrine:database:create --env=test
    php bin/console doctrine:migrations:migrate --env=dev
    php bin/console doctrine:migrations:migrate --env=test
    php bin/console doctrine:fixtures:load
    ```

6. **Create file storage folder:**
    ```bash
    mkdir var/files
    ```

7.  **Start the server:**
    ```bash
    symfony server:start
    ```

The API will be running at `http://127.0.0.1:8000`.

---

## 🐍 The Python API (in progress, this branch only)

This branch carries a rewrite of the API in Python — FastAPI, SQLAlchemy and Alembic
— following the design in the umbrella repository's `ARCHITECTURE.md`. It is not
finished and is not on `main`. The Symfony application above is still the released
one, and both run from this working tree in the meantime.

Working so far: authentication, access control on `/api`, text generation, the file and
directory routes over the story repository, and account management from the shell.

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

The default database is `var/data_dev.db`. A database that already holds `user` and
`refresh_token` tables satisfies the first revision as it stands, so record it as
applied rather than running it:

```bash
poetry run alembic stamp 9755af1d75c1
```

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

## 🧪 API Endpoints

A Postman collection or OpenAPI/Swagger documentation will be available soon. Here are the main endpoints:

### Auth
- `POST /auth`: Authenticate and receive a JWT.

### Files & Directories
- `GET /api/tree`: Get the full file and directory structure for the user.
- `POST /api/file`: Create a new file.
- `GET /api/file/{id}`: Get details for a specific file.
- `PUT /api/file/{id}`: Update a file's details (e.g., name, parent directory).
- `DELETE /api/file/{id}`: Delete a file.
- `POST /api/dir`: Create a new directory.
- `GET /api/dir/{id}`: Get details for a specific directory and its contents.
- `PUT /api/dir/{id}`: Update a directory's details (e.g., name, summary).
- `DELETE /api/dir/{id}`: Delete a directory.

### Text Generation
- `POST /api/ollama/generate`: Get a completion for the provided text using the provided model.

---

## 🧪 Quality and Testing

This project uses **AI-assisted development** for rapid implementation, with human oversight and validation. Automated tests are in place to ensure API correctness and reliability.

Run the test suite:
```bash
php bin/phpunit
```

---

## 📜 License

This project is released under the [MIT License](../inkspire-frontend/LICENSE).
It is provided *as is*, without warranty, but every effort is made to ensure code reliability and responsible use of AI-generated components.

---

## 💬 Acknowledgments

InkSpire is based on a code developped by:

- [Evann Abrial](https://www.linkedin.com/in/evann-abrial-26b446297/)
- [Lola Chalmin](https://www.linkedin.com/in/lola-chalmin-112ab9290/)
- [Roxane Rossetto](https://www.linkedin.com/in/roxane-rossetto-3b9158211/)

---

*© 2025 InkSpire. Built with care, code, and a bit of inkSpiration.*
