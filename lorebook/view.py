"""Render the RDF graph as a self-contained, interactive HTML page.

The graph is transformed into a ``{nodes, links}`` structure in Python (rdflib),
then embedded *inline* into a single HTML file alongside a vendored copy of the
`force-graph <https://github.com/vasturiano/force-graph>`_ engine. The result is
a standalone file that opens straight from ``file://`` -- no server, no network,
no external requests.

Security notes (this viewer is a hardened re-implementation of the pattern used
by BrickStudio, whose original had three issues we deliberately avoid):

* **No CORS proxy / no data exfiltration.** BrickStudio fetched remote Turtle
  through ``cors-anywhere.herokuapp.com``. Here the data never leaves the
  machine: it is produced locally and embedded in the page.
* **No DOM XSS.** Graph-derived text reaches the DOM only as escaped strings
  (see ``esc()`` in the template) or as canvas ``fillText`` -- never as raw
  ``innerHTML``. The embedded JSON is delimited-escaped (``<``/``>``/``&``) and
  read via ``JSON.parse``, never ``eval``.
* **No stale CDN/third-party calls.** The one third-party dependency
  (force-graph, which bundles d3-force) is a pinned, current UMD build vendored
  into the repo and inlined; the page loads no fonts or scripts over the wire.
"""

from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

from jinja2 import Environment
from rdflib import Graph, Literal, URIRef
from rdflib.term import Node

from .ontology import CORE, RDF, SDO, Vocabulary

#: Literal predicates whose local name starts with this are long prose blocks
#: (``lore:sectionPersonality`` etc.) -- useful in the Markdown export, but too
#: bulky for graph tooltips, so we drop them from the embedded node attributes.
_PROSE_PREFIX = "section"


def _local(term: Node) -> str:
    """Short local name of a URI (fragment or last path segment)."""
    return str(term).rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def build_view_data(graph: Graph, vocab: Vocabulary) -> dict:
    """Transform ``graph`` into a ``{"nodes": [...], "links": [...]}`` dict.

    * A **node** is any entity IRI appearing as a subject, or as the object of a
      non-``rdf:type`` edge. Ontology class IRIs (only ever ``rdf:type`` objects)
      are therefore excluded automatically.
    * A **link** is any triple whose object is an IRI (other than ``rdf:type``).
    * ``rdf:type`` objects become a node's ``types``; literal objects become
      ``attrs`` (prose sections excluded).
    * ``type`` is the node's **most specific** class -- the deepest in the
      ``rdfs:subClassOf`` hierarchy, ties broken alphabetically. Because subclass
      types are materialized (a SecondaryCharacter is also a Character), picking
      the shallowest or alphabetically-first class would collapse every subclass
      into its parent and make it unfilterable in the legend.
    """
    types: dict[str, list[URIRef]] = {}
    attrs: dict[str, dict[str, list[str]]] = {}
    subjects: set[str] = set()
    edge_endpoints: set[str] = set()
    links: list[dict] = []

    for subj, pred, obj in graph:
        if not isinstance(subj, URIRef):
            continue
        sid = str(subj)
        subjects.add(sid)

        if pred == RDF.type:
            if isinstance(obj, URIRef):
                types.setdefault(sid, []).append(obj)
        elif isinstance(obj, URIRef):
            oid = str(obj)
            edge_endpoints.add(oid)
            links.append({"source": sid, "target": oid, "label": _local(pred)})
        elif isinstance(obj, Literal):
            local = _local(pred)
            if not local.startswith(_PROSE_PREFIX):
                attrs.setdefault(sid, {}).setdefault(local, []).append(str(obj))

    # Hierarchy depth of a class, cached: the number of classes in its
    # rdfs:subClassOf closure (Character = 2, SecondaryCharacter = 3, ...).
    depths: dict[URIRef, int] = {}

    def _depth(cls: URIRef) -> int:
        if cls not in depths:
            depths[cls] = len(vocab.class_closure(cls))
        return depths[cls]

    node_ids = subjects | edge_endpoints
    degree: dict[str, int] = {nid: 0 for nid in node_ids}
    for link in links:
        degree[link["source"]] = degree.get(link["source"], 0) + 1
        degree[link["target"]] = degree.get(link["target"], 0) + 1

    nodes = []
    for nid in sorted(node_ids):
        # Subclass closures mean nearly every node is also a core:Entity. That
        # universal root carries no information next to a real type, so it is
        # shown only when it is all we know about a node.
        type_iris = types.get(nid, [])
        specific = [t for t in type_iris if t != CORE.Entity] or type_iris
        node_types = sorted(_local(t) for t in specific)
        primary = min(specific, key=lambda t: (-_depth(t), _local(t))) if specific else None
        name = graph.value(URIRef(nid), SDO.name)
        nodes.append(
            {
                "id": nid,
                "label": str(name) if name is not None else _local(URIRef(nid)),
                "type": _local(primary) if primary is not None else "Unknown",
                "types": node_types,
                "attrs": attrs.get(nid, {}),
                "degree": degree.get(nid, 0),
            }
        )

    return {"nodes": nodes, "links": links}


def _embed_json(data: dict) -> str:
    """Serialize ``data`` for safe inclusion in a ``<script>`` element.

    ``<``/``>``/``&`` are escaped to their ``\\uXXXX`` forms so the payload can
    never terminate the script element or be reinterpreted as markup, while
    remaining valid JSON for ``JSON.parse``.
    """
    raw = json.dumps(data, ensure_ascii=False)
    return raw.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _vendored_force_graph() -> str:
    return resources.files("lorebook").joinpath("vendor", "force-graph.min.js").read_text("utf-8")


def render_html(graph: Graph, vocab: Vocabulary, *, title: str = "Lorebook") -> str:
    """Render the full self-contained HTML page for ``graph``."""
    data = build_view_data(graph, vocab)
    template_text = resources.files("lorebook").joinpath("templates", "view.html.j2").read_text("utf-8")
    template = Environment(autoescape=True).from_string(template_text)
    return template.render(
        title=title,
        data_json=_embed_json(data),
        force_graph_js=_vendored_force_graph(),
        node_count=len(data["nodes"]),
        link_count=len(data["links"]),
    )


def write_view(graph: Graph, vocab: Vocabulary, out_path: str | Path, *, title: str = "Lorebook") -> Path:
    """Render the page and write it to ``out_path`` (creating parent dirs)."""
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(graph, vocab, title=title), encoding="utf-8")
    return out
