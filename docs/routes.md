# Routes

Every route the API serves, 47 in all, with what it takes, what it answers and how it fails. The
routers are in `inkspire_api/`: `main.py` (`/api/me`), `auth.py`, `files.py`, `documents.py`, `activity.py`, `llm.py`, `titles.py`,
`completions.py`, `timelines.py`, `lore.py` and `repository.py`. This document lists what those
files declare, and `tests/test_routes_doc.py` fails if a route is added or removed without this
document changing.

## Conventions

**Authentication.** Everything under `/api` needs a valid token, sent as the `jwt_token` cookie or
as `Authorization: Bearer <token>`. `/auth`, `/auth/refresh` and `/auth/logout` are the only
routes outside `/api`, and the only ones that need no token.

**Two roots.** The stories root is served under `/api/stories` and the notes root under
`/api/notes`. Where a route exists on both, the table says so, and the notes root answers the same
shape as the stories one unless this document says otherwise. A directory is a story under
`/api/stories` and a folder under `/api/notes`; both are written `dir` in a path and in a body.
Directories are one level deep in both roots.

**Ids.** An id is `blake2b(space + "\0" + root-relative path)[:16]`, 16 hexadecimal characters. The
space is part of what is hashed, so the same relative path in the two roots is two ids, and an id
from one root answers 404 under the other. Renaming a file changes its id; renaming a story or a folder does not. A client
never sends a path. See [`filesystem.md`](filesystem.md).

**Errors.** Every error is `{"code": <status>, "message": <text>}`. Deleting a story that holds
more than its chapters adds `"holds": [<names>]`. These statuses are used throughout:

| Status | Meaning |
|---|---|
| 400 | The body is not valid. `message` is the first problem, as `field: reason`. |
| 401 | No token, an invalid token, or wrong credentials. |
| 404 | No file or directory in that root has the id, or the story has no timeline or lorebook. |
| 409 | The operation would destroy something, or git refused it. |
| 413 | A file body over 4 MiB. |
| 422 | A file is there and cannot be read as what it claims to be, or the request is understood and cannot be done. `message` says why. |
| 429 | A rate limit. |
| 500 | The server's configuration is wrong, or a model provider could not be reached. |
| 502 | A git remote failed, or a model answered with nothing usable. |
| 504 | A git command did not finish in time. |

**Limits on text.** A name is 1 to 255 characters after trimming. A summary or a synopsis is at most
2000 characters. A status is at most 64 characters, and `""` removes it. A commit message is 1 to
2000 characters. A generation instruction is at most 300 characters.

## Authentication

| Route | Takes | Answers |
|---|---|---|
| `POST /auth` | `{"username", "password"}`; `username` is the account's email | `{"token"}`, and sets the cookies `jwt_token` (httpOnly, path `/`), `refresh_token` (httpOnly, path `/auth`) and `auth_status=1` (readable, path `/`), all `SameSite=Strict` |
| `POST /auth/refresh` | the `refresh_token` cookie | `{"success": true}`, a new `jwt_token`, and a rotated `refresh_token`; the previous one stops working |
| `POST /auth/logout` | the `refresh_token` cookie, if any | `{"success": true}`; revokes the refresh token and clears the three cookies; succeeds when there was no session |
| `GET /api/me` | | `{"email", "roles"}`: the account the request is authenticated as |

`POST /auth` answers 401 for wrong credentials and 429 after repeated failures from one client
address. `POST /auth/refresh` answers 401 when the cookie is missing, unknown or expired.

## Directories

Each route exists under `/api/stories` and `/api/notes`.

| Route | Takes | Answers |
|---|---|---|
| `GET /tree` | | `{"user", "dirs", "files"}`: every directory with its files, and every file at the root. Stories: `files` holds one-shots. Notes: `files` holds loose notes. Both lists are in the order to draw, and a story's chapters are in `story.yaml` order. A file entry is `{"id", "name", "status", "modified"}`; a directory entry is `{"id", "name", "summary", "modified", "files"}`, and a story also carries `"timeline"` and `"lorebook"`. `modified` is when the file last changed, or, for a directory, the newest of its manifest's and its files' times: an ISO 8601 string in UTC with whole seconds and a trailing `Z` (`"2026-10-25T09:14:07Z"`), or `null` when nothing can be read. It is the file's modification time on the server, so a `git pull` sets it to the moment of the pull, and a file edited outside the API shows that edit |
| `GET /dir/{dir_id}` | | One directory as in the tree, with `summary` on each of its files too. A story also carries `timeline` and `lorebook`, which say whether the files those views read exist |
| `POST /dir` | `{"name", "summary"?}` | 201 `{"id", "name", "summary"}` |
| `PUT /dir/{dir_id}` | `{"name"?, "summary"?}`; an absent field is left as it is | `{"id", "name", "summary"}`. The id does not change: the directory keeps its slug |
| `DELETE /dir/{dir_id}` | `?force=true` (stories only) | 204. A story is refused with 409 and `holds` while it contains anything besides `story.yaml` and `chapters/`; `force=true` deletes it whole. A folder is refused with 409 while it contains anything besides its files and its manifest |
| `PUT /api/stories/dir/{dir_id}/chapters` | `{"order": [<chapter ids>]}` | The story as `GET /dir/{dir_id}` answers it, `modified` included. Writes `story.yaml` only. A chapter the list leaves out stays after the ones named; the last write wins |

## Files

Each route exists under `/api/stories` and `/api/notes`. Under `/api/stories` a file is a chapter or
a one-shot; under `/api/notes` it is a note.

| Route | Takes | Answers |
|---|---|---|
| `POST /file` | `{"dir", "name"}`; `dir` is a directory id or `null` | 201 `{"id", "name", "dir"}`. Stories: `dir: null` creates a one-shot. Notes: `dir: null` creates a note at the root |
| `GET /file/{file_id}` | | `{"id", "name", "status", "summary", "modified"}`, with `modified` as in a tree's file entry |
| `PUT /file/{file_id}` | `{"name"?, "dir"?, "status"?, "summary"?}` | `{"id", "name", "dir", "status", "summary"}`. A rename or a move changes the id; a status or a summary alone does not. `status` or `summary` of `""` removes the key from the file's header. Stories: a chapter's `dir` moves it to another story, `null` leaves it where it is, and a `dir` given for a one-shot is refused with 422. Notes: an absent `dir` leaves the note where it is and `null` moves it to the root |
| `DELETE /file/{file_id}` | | 204 |
| `GET /file/{file_id}/contents` | | The prose as `text/plain`, without the header or any section |
| `GET /file/{file_id}/document` | | `{"body", "metadata", "reconciled"}`. `metadata` maps a paragraph's hash to its `[start, end, kind]` runs, and is `null` when the file has no provenance. `reconciled` is `null`, or `{"revision", "recovered", "reset", "dropped"}` listing the paragraphs whose records a hand edit left stale and what was done about each. Stories: recovered from git history. Notes: a stale record is dropped. Reading writes nothing |
| `PUT /file/{file_id}/document` | `{"body", "metadata"}`; `metadata: null` removes the section | `{"ok": true}`. Writes the prose and the provenance in one write. 413 for a body over 4 MiB. Afterwards, in the background, a context summary may be requested (see [`prompt.md`](prompt.md)) |

Under `/api/stories` only:

| Route | Takes | Answers |
|---|---|---|
| `GET /api/stories/file/{file_id}/history` | `?limit=` from 1 to 200, default 50 | A list, newest first, of `{"sha", "short", "author", "date", "message"}`, following the chapter across a rename. 404 for an id that is not a chapter, a one-shot's included |
| `GET /api/stories/file/{file_id}/at/{rev}` | `rev`: 7 to 40 hexadecimal characters | The chapter's prose at that commit as `text/plain`. 404 when the commit is not in the chapter's history |

## Generation

| Route | Takes | Answers |
|---|---|---|
| `GET /api/llm/models` | | A list of `{"name": "<provider>/<model>", "protocol": "openai" or "ollama"}`. A provider that cannot be reached contributes nothing. Held for an hour per provider |
| `GET /api/llm/defaults` | | The server's own generation settings: `temperature`, `prompt_budget`, `prefix_share`, `num_ctx`, `think`, `send_selection` |
| `GET /api/llm/features` | | `{"title": bool, "summary": bool}`; both are `true` exactly when `INKSPIRE_LLM_SMALL_MODEL` is set |
| `POST /api/stories/file/{file_id}/generate` | see below | `text/event-stream` |
| `POST /api/notes/file/{file_id}/generate` | see below | `text/event-stream` |

The body of `generate` is `{"model"}` and, optionally, `think`, `temperature` (0 to 2),
`prompt_budget`, `prefix_share` (0 to 1), `num_ctx`, `send_selection`, `instruction`, and the cursor
fields: `cursor_para` with `cursor_offset`, and `cursor_end_para` with `cursor_end_offset`. Each of
the first six overrides the server's setting for this request alone. A request carries no text: the
server reads the file as saved. The cursor fields choose a continuation (neither pair, or a caret
at the end of the file), a fill-in-the-middle (a caret elsewhere) or a rewrite (both pairs); one
field of a pair without the other is refused. The events are `{"delta": "<text>"}`, then
`[DONE]`; `{"error": "<text>"}` if the provider fails after the stream has started. Before any text,
a failure is a status: 422 for an unknown model or a cursor out of range or inverted, 429 over 20
generations per minute per account, 500 for a provider that cannot be reached. Nothing is written
to the file. The prompt is described in [`prompt.md`](prompt.md).

Two shorter calls answer in one response:

| Route | Takes | Answers |
|---|---|---|
| `POST /api/stories/file/{file_id}/title` | `{"instruction"?}`, or no body | `{"title"}`. Uses `INKSPIRE_LLM_SMALL_MODEL`; 409 when it is not set. 422 for an empty file, 429 over the generation limit it shares, 502 when the model answers with nothing usable |
| `POST /api/stories/file/{file_id}/complete`, `POST /api/notes/file/{file_id}/complete` | `{"model"}` | `{"completion"}`: up to `INKSPIRE_LLM_COMPLETION_TOKENS` tokens, cut at the first complete sentence. 422 for an empty file or an unknown model, 429 over its own limit (`INKSPIRE_LLM_COMPLETION_LIMIT` per interval), 502 when the model answers with nothing usable |

## A story's timeline and lorebook

Read-only, one story each. Neither writes anything: both are built in memory from
the YAML the writer authored, and held per story until one of the files changes. 404 when the story
has no `timeline.yaml` or no `lorebook/lorebook.yaml`; 422 with the reason when the file is there and
cannot be read.

| Route | Answers |
|---|---|
| `GET /api/stories/dir/{dir_id}/timeline` | `{"title", "maxHeight", "widthStep", "characters", "events", "arcs"}`. A character is `{"key", "name", "color", "events"}`, its events as event keys in date order. An event is `{"key", "date", "description", "characters", "href", "x1", "y1", "x2", "y2"}`, with `date` already formatted for display and the order of `events` the order in time. An arc is `{"name", "firstEvent", "lastEvent"}` |
| `GET /api/stories/dir/{dir_id}/lore/graph` | `{"nodes", "links"}`. A node is `{"id", "label", "type", "types", "attrs", "degree"}`, with no prose. A link is `{"source", "target", "label"}` |
| `GET /api/stories/dir/{dir_id}/lore/entity/{local}` | One entity: `id`, `local`, `types`, `aka`, its fields, its relations as labels, and `sections`, the prose in template order. 404 when nothing in the lorebook has that name |

## Activity

| Route | Takes | Answers |
|---|---|---|
| `GET /api/stories/activity` | `?days=` from 1 to 366, default 365 | A list, oldest first, of `{"date": "YYYY-MM-DD", "words": <count>}`, one entry per day that has any. The words are what the day's commits added to chapters and one-shots: the words in the new body that the old one lacked. Only committed work counts; a chapter typed today and not yet committed has no day. A day is the commit's author date, in its own offset, so one commit after a week of writing puts the whole week on one day. A rewrite counts the words it adds even when the passage ends no longer. Notes are not counted (the notes root is not a repository). A renamed chapter is not new prose. 401 without a token, like every `/api` route; 400 for a `days` outside the range. Only the newest 5000 commits of the window are read, so a history with more than that loses its oldest days. The answer is held against the repository's `HEAD` and today, and rebuilt only when either moves |

## Git

The story repository, at `INKSPIRE_DATA_ROOT`. The notes root is not a repository. Commit, push and
pull share one non-blocking lock: a second one while the first runs is refused with 409, not queued.

| Route | Takes | Answers |
|---|---|---|
| `GET /api/git/status` | | `{"branch", "upstream", "ahead", "behind", "fetched", "clean", "changes"}`. `ahead` and `behind` are `null` without an upstream and are only as fresh as the last pull; `fetched` is always `false`. A change is `{"path", "state", "staged", "dir", "committable"}`. 409 when `HEAD` is not on a branch |
| `POST /api/git/commit` | `{"message"}` | The status, and `sha`, `message` and `committed`. Commits only a story's `story.yaml` and its `chapters/*.ink`; anything else changed, or staged by hand, is left as it was. 409 when nothing committable has changed |
| `POST /api/git/push` | | The new status. 409 without an upstream, or when the remote has commits the branch does not; 502 when the remote cannot be reached |
| `POST /api/git/pull` | | The new status. Fast-forward only: 409 without an upstream or when the branch has diverged; 502 when the remote cannot be reached |
