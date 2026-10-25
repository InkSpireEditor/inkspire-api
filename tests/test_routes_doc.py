# -*- coding: utf-8 -*-
"""`docs/routes.md` lists every route the application serves, and nothing it does not.

A route that exists under both `/api/stories` and `/api/notes` may be written once in the
document, without the prefix, as the tables there do. Anything else is written in full.
"""

from __future__ import annotations

import re
from pathlib import Path

DOC = Path(__file__).resolve().parent.parent / "docs" / "routes.md"
METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH")
SHARED_PREFIXES = ("/api/stories", "/api/notes")


def served(app) -> dict[str, set[str]]:
    """Every route as `METHOD path`, mapped to the spellings the document may use.

    Read from the OpenAPI schema, which lists a route once per path and method wherever
    its router was included from.
    """
    spellings: dict[str, set[str]] = {}
    for path, operations in app.openapi()["paths"].items():
        for method in operations:
            method = method.upper()
            if method not in METHODS:
                continue
            written = {f"{method} {path}"}
            for prefix in SHARED_PREFIXES:
                if path.startswith(prefix + "/"):
                    written.add(f"{method} {path[len(prefix):]}")
            spellings[f"{method} {path}"] = written
    return spellings


def documented() -> set[str]:
    text = DOC.read_text(encoding="utf-8")
    return set(re.findall(r"`((?:%s) /[^`\s]*)`" % "|".join(METHODS), text))


def test_every_route_is_in_the_document(app) -> None:
    listed = documented()
    missing = [route for route, written in served(app).items() if not written & listed]
    assert not missing, f"docs/routes.md does not list: {missing}"


def test_the_document_lists_no_route_that_does_not_exist(app) -> None:
    allowed = {spelling for written in served(app).values() for spelling in written}
    unknown = sorted(documented() - allowed)
    assert not unknown, f"docs/routes.md lists routes the application does not serve: {unknown}"


def test_the_document_lists_the_number_of_routes_it_says(app) -> None:
    """The introduction states how many routes there are; a count that has gone stale is wrong."""
    text = DOC.read_text(encoding="utf-8")
    match = re.search(r"(\d+) in all", text)
    assert match is not None, "docs/routes.md no longer states how many routes there are"
    assert int(match.group(1)) == len(served(app))


def test_a_route_written_without_its_prefix_exists_under_both_roots(app) -> None:
    """Written once without `/api/stories` or `/api/notes` means it is served under both."""
    routes = set(served(app))
    for listed in documented():
        method, path = listed.split(" ", 1)
        if path.startswith(("/api/", "/auth")):
            continue
        for prefix in SHARED_PREFIXES:
            assert f"{method} {prefix}{path}" in routes, f"{listed} is not served under {prefix}"
