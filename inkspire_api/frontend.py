# -*- coding: utf-8 -*-
"""Serves the built frontend, so one process answers the pages and the API.

Only used when `INKSPIRE_FRONTEND_DIR` is set. Every path that is not a file of the
build answers `index.html`, which is how the frontend's own routes (`/stories/<id>`)
load when typed into the address bar. A path under `/api` or `/auth` never does: a
mistyped API route answers a JSON 404, not a page.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response

#: The hashes of the three `<style>` blocks that the libraries behind the lorebook graph
#: (force-graph and what it uses for tooltips and the drawing area) inject when they load,
#: through their `style-inject` helper. The browser reports a block it refuses with its hash,
#: so a dependency update that changes one shows up in the console of a page that loads the
#: graph, and the new hash is added here. The page still works without it; the tooltip and
#: the graph's container lose their styles.
INJECTED_STYLES = (
    "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU=",
    "sha256-9xjtvxMT1ApHlgn9ohbh2FNfvK5Tqtzy94BjfXBeMSY=",
    "sha256-jtRRMYY2VbEXuBKbHvqM1/fPT8vazFrEooXdvwUK4y8=",
)

#: What a page may load and from where. The build loads its script, its stylesheet and its
#: images from this origin and talks only to this origin. No script is inline, the only
#: inline styles are the hashed blocks above, nothing is framed, and no `<base>` or form can
#: send a visitor elsewhere.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; "
    + "style-src 'self' "
    + " ".join(f"'{digest}'" for digest in INJECTED_STYLES)
    + "; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)

#: Vite names a file under `assets/` after its content, so a browser may keep it for good.
IMMUTABLE = "public, max-age=31536000, immutable"

#: The first segments that belong to the API.
API_PREFIXES = ("api", "auth")


def is_api_path(path: str) -> bool:
    """Whether `path` (without its leading slash) is the API's, even if no route matches."""
    return path.split("/", 1)[0] in API_PREFIXES


def resolve(root: Path, path: str) -> Path:
    """The file to answer for `path`: the one it names inside `root` when it is a regular
    file, `index.html` otherwise. A path that leaves `root`, by `..` or by a symlink, is
    not a file of the build."""
    candidate = (root / path).resolve()
    if candidate.is_relative_to(root) and candidate.is_file():
        return candidate
    return root / "index.html"


def mount(application: FastAPI, directory: Path) -> None:
    """Adds the catch-all that serves `directory`. Register it after every other route."""
    root = directory.resolve()
    if not (root / "index.html").is_file():
        raise RuntimeError(f"INKSPIRE_FRONTEND_DIR {root} holds no index.html.")

    @application.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def serve(path: str) -> Response:
        if is_api_path(path):
            raise HTTPException(status_code=404, detail="Not found")
        target = resolve(root, path)
        in_assets = target.parent == root / "assets"
        headers = {
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "Cache-Control": IMMUTABLE if in_assets else "no-cache",
        }
        return FileResponse(target, headers=headers)
