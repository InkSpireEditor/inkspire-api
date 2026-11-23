# Deployment

One container image holds the API and the built frontend. FastAPI serves both on port 8000, so
there is one process and one origin: the frontend calls `/api` and `/auth` on the address it was
loaded from, and no CORS answer is involved.

The image is `ghcr.io/inkspireeditor/inkspire`. Pushing a tag `vX.Y.Z` to this repository builds
it from this repository and `inkspire-frontend` at the same tag, and publishes `:X.Y.Z`, `:X.Y`
and `:latest`. A push to `main` publishes nothing.

## What is in the image

- `/app`: the API, `lorebook`, `timeline`, the migrations, the committed `.env` and the virtual
  environment. `/app/frontend` is the frontend build. Both belong to root and the process cannot
  write to them.
- `git` and `openssh-client`, which the story repository's push and pull need. Nothing else
  from the distribution is added.
- No `providers.yaml`, no `.env.local`, no story, no database and no key of any kind.
  `.dockerignore` lists what the build may take, so a file added to the repository later is left
  out until someone adds it there.
- The process runs as uid/gid 1001, `inkspire`, with no login shell.

## What to provide

**One writable volume, mounted at `/data`.** The entrypoint creates what it needs inside it.

| Path | Holds |
|---|---|
| `/data/db/inkspire.db` | the accounts and the refresh tokens (SQLite) |
| `/data/files` | the notes |
| `/data/story-repo` | the story repository, a git working tree. Clone it here before the first start |
| `/data/home` | `HOME` of the process: git's configuration and the SSH key and `known_hosts` for the repository's remote |

The volume has to be writable by uid 1001.

**Secrets, as files in `/run/secrets`.** `INKSPIRE_SECRETS_DIR` points there. A file is named
after the setting with its prefix, and the name is matched without regard to case.

| File | Holds |
|---|---|
| `INKSPIRE_JWT_SECRET` | the signing secret, required. 64 hexadecimal characters from `python -c 'import secrets; print(secrets.token_hex(32))'` |
| `providers.yaml` | the providers and their keys, optional. `INKSPIRE_LLM_PROVIDERS_FILE` points at it |

A secret in the environment wins over a file. Prefer the file: a container's environment can be
read by whoever can inspect the container.

**Environment the image sets, and why.**

| Variable | Value | Why |
|---|---|---|
| `INKSPIRE_FRONTEND_DIR` | `/app/frontend` | serve the build |
| `INKSPIRE_COOKIE_SECURE` | `true` | the cookies always carry `Secure`, whatever scheme the request seemed to arrive over |
| `INKSPIRE_EXPOSE_DOCS` | `false` | `/docs`, `/redoc` and `/openapi.json` need no login and list every route |
| `INKSPIRE_DATABASE_URL`, `INKSPIRE_FILES_ROOT`, `INKSPIRE_DATA_ROOT` | paths under `/data` | the volume above |
| `HOME` | `/data/home` | git and ssh write their configuration there |

**Settings the image leaves for the deployment.** The committed `.env` is copied into the image,
so what it sets (`INKSPIRE_LLM_NUM_CTX`, the prompt budget, the prefix share, the timeout) is
already in effect. Three groups are not set there, because the right value depends on the
installation. Set them with `-e`; none of them is a secret.

| Variable | Without it |
|---|---|
| `INKSPIRE_LLM_SMALL_MODEL`, as `<provider>/<model>` | `GET /api/llm/features` answers `title: false` and `summary: false`. The editor shows no title button and no title field in the edit dialog, and no context summary is written. The provider has to be in `providers.yaml`. |
| `INKSPIRE_LLM_THINK` (`true` or `false`) | Nothing is sent, and each model uses its own default. A reasoning model thinks, delaying the first chunk of a generation by the whole reasoning pass, and can spend a small context window on it and answer nothing. Title and completion requests send `false` whatever this is. |
| `INKSPIRE_GIT_AUTHOR_NAME` and `INKSPIRE_GIT_AUTHOR_EMAIL` | A commit made from the editor uses the `user.name` and `user.email` that git finds under `/data/home` or in the story repository. If there are none, the commit is refused and the message names these two settings. |

`INKSPIRE_LLM_SMALL_MODEL` also turns on the context summary, a model call after the save of any
chapter longer than the prompt budget, so it is a cost on a slow provider.

**The story repository's remote.** Push and pull run `ssh` inside the container, and the
`known_hosts` file it reads decides which servers it accepts. With `StrictHostKeyChecking=yes`
and a `known_hosts` file given by `UserKnownHostsFile`, an entry is matched against the host name
written in the remote's URL. A `known_hosts` line for `gitea` does not cover a remote
`git@git.example.org:owner/repo.git`, and the pull fails with `No ED25519 host key is known for
git.example.org`. The error comes from the container, not the host running it. Either clone with
the name that has an entry, or add an entry for the name the URL uses, taking the key from
`ssh-keyscan -t ed25519 <name>` and checking its fingerprint against the server's. A remote's URL
is in `/data/story-repo/.git/config`; `git -C /data/story-repo remote set-url origin <url>` changes it.

**Behind a reverse proxy,** set `FORWARDED_ALLOW_IPS` to the proxy's address and nothing wider.
uvicorn then reads `X-Forwarded-For` and `X-Forwarded-Proto` from that address only. Without it
every client has the proxy's address, and the login throttle, which counts attempts per client
address, counts all of them together. With `*`, any client can write the header and choose the
address the throttle sees.

## Running it

```bash
docker run -d --name inkspire \
  --read-only --tmpfs /tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -e FORWARDED_ALLOW_IPS=<proxy address> \
  -v inkspire-data:/data \
  -v /path/to/secrets:/run/secrets:ro \
  ghcr.io/inkspireeditor/inkspire:X.Y.Z
```

The entrypoint applies the migrations (`alembic upgrade head`) and starts `inkspire run` on
`0.0.0.0:8000`. Accounts are made inside the container:

```bash
docker exec -it inkspire inkspire user create you@example.com
```

The image serves plain HTTP. TLS ends at the proxy.

## Checking an image before it runs

Each published image has a build provenance attestation signed by this repository's workflow,
and an SBOM. With the GitHub CLI:

```bash
gh attestation verify oci://ghcr.io/inkspireeditor/inkspire:X.Y.Z -R InkSpireEditor/inkspire-api
```

A tag can be moved and a digest cannot, so a deployment that wants to run exactly the image it
verified refers to `@sha256:...`.

## Repository settings

These are not in the code and have to be set once in GitHub:

- A tag ruleset on `v*`, so that only the maintainer can create a release tag. The workflow
  publishes whatever such a tag points at.
- Actions' default workflow permissions set to read. The release workflow asks for what it
  needs, job by job.
- After the first publish, the package's visibility, and its link to this repository, which is
  what lets the workflow's own token keep writing to it.
