# One image with the API and the built frontend. See docs/deployment.md.
#
# The build context is this repository with the matching frontend checkout in
# `frontend-src/` (the release workflow puts it there). Every base image is pinned by
# digest; Dependabot moves the digests.

# --- the frontend ---------------------------------------------------------
FROM node:22-alpine@sha256:fe4f6d74e20370acb04ad889077d14d0f64a9c8ea6a42bc60cddf1e844699914 AS frontend
WORKDIR /src
# --ignore-scripts: no install script of any dependency runs during the build.
COPY frontend-src/package.json frontend-src/package-lock.json ./
RUN npm ci --ignore-scripts
COPY frontend-src/ ./
# The page and the API share an origin, so the URLs are paths. A variable in the
# environment wins over the frontend's committed `.env`.
ENV VITE_API_URL=/api VITE_AUTH_URL=/auth
RUN npm run build-only

# --- the API's dependencies ----------------------------------------------
FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8 AS build
WORKDIR /app
# Poetry installs from poetry.lock and stays in this stage.
RUN pip install --no-cache-dir poetry==2.4.1 \
    && poetry config virtualenvs.in-project true
COPY pyproject.toml poetry.lock LICENSE ./
RUN poetry install --only main --no-root --no-interaction --no-ansi
COPY inkspire_api/ ./inkspire_api/
COPY lorebook/ ./lorebook/
COPY timeline/ ./timeline/
RUN poetry install --only main --no-interaction --no-ansi

# --- what runs --------------------------------------------------------------
FROM python:3.14-slim@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8 AS runtime

LABEL org.opencontainers.image.title="InkSpire" \
      org.opencontainers.image.source="https://github.com/InkSpireEditor/inkspire-api" \
      org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"

# git for the story repository (GitPython runs it); openssh-client for its remote. The
# last step removes every setuid and setgid bit, which nothing here needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1001 inkspire \
    && useradd --uid 1001 --gid 1001 --no-create-home --home-dir /data/home \
         --shell /usr/sbin/nologin inkspire \
    && mkdir /data \
    && chown inkspire:inkspire /data \
    && find / -xdev -type f \( -perm -4000 -o -perm -2000 \) -exec chmod a-s {} +

# Owned by root and read-only to the process: it cannot rewrite its own code.
WORKDIR /app
COPY --from=build /app/.venv ./.venv
COPY --from=build /app/pyproject.toml ./pyproject.toml
COPY --from=build /app/inkspire_api ./inkspire_api
COPY --from=build /app/lorebook ./lorebook
COPY --from=build /app/timeline ./timeline
COPY alembic ./alembic
COPY alembic.ini .env ./
COPY docker_entrypoint.sh /usr/local/bin/docker_entrypoint.sh
COPY --from=frontend /src/dist ./frontend

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/data/home \
    INKSPIRE_FRONTEND_DIR=/app/frontend \
    INKSPIRE_COOKIE_SECURE=true \
    INKSPIRE_EXPOSE_DOCS=false \
    INKSPIRE_DATABASE_URL=sqlite:////data/db/inkspire.db \
    INKSPIRE_FILES_ROOT=/data/files \
    INKSPIRE_DATA_ROOT=/data/story-repo \
    INKSPIRE_LLM_PROVIDERS_FILE=/run/secrets/providers.yaml \
    INKSPIRE_SECRETS_DIR=/run/secrets

VOLUME /data
USER inkspire
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=3)"]

ENTRYPOINT ["docker_entrypoint.sh"]
CMD ["inkspire", "run", "--host", "0.0.0.0", "--port", "8000"]
