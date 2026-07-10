"""Compact graph-backed orientation cards for codeintel targets."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeintel import OUT_DIR, TARGETS_DIR
from codeintel.config import ConfigError, load_target_by_name
from codeintel.core.graph import KuzuGraphStore
from codeintel.monitor import latest_snapshot_info


class BriefError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    kind: str
    props: dict[str, Any]


@dataclass
class Graph:
    nodes: dict[str, dict[str, Any]]
    out: dict[str, list[Edge]]
    inc: dict[str, list[Edge]]


def cli_run(args: argparse.Namespace) -> int:
    try:
        target = load_target_by_name(args.target, TARGETS_DIR)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    db = OUT_DIR / target.name / "graph.kuzu"
    if not db.exists():
        print(f"error: no graph at {db}; run `codeintel index {target.name}` first", file=sys.stderr)
        return 1
    try:
        print(render_brief(target.name, db, args.about))
    except BriefError as e:
        print(str(e), file=sys.stderr)
        return e.code
    return 0


def render_brief(target_name: str, db_path: Path, about: str) -> str:
    graph = _load_graph(db_path)
    node_id = _resolve_about(graph, about)
    node = graph.nodes[node_id]
    kind = node["kind"]
    if kind == "Route":
        return _render_route(target_name, graph, node_id)
    if kind == "File":
        return _render_file(target_name, graph, node_id)
    if kind in {"Function", "Class"}:
        return _render_symbol(target_name, graph, node_id)
    if kind == "Component":
        return _render_component(target_name, graph, node_id)
    raise BriefError(f"no brief renderer for {kind} node {node_id}")


def _load_graph(db_path: Path) -> Graph:
    store = KuzuGraphStore(db_path)
    try:
        node_rows = store.query(
            "MATCH (n:Node) "
            "RETURN n.id AS id, n.kind AS kind, n.name AS name, n.qname AS qname, "
            "n.path AS path, n.line AS line, n.props AS props ORDER BY n.id"
        )
        edge_rows = store.query(
            "MATCH (a:Node)-[e:Edge]->(b:Node) "
            "RETURN a.id AS src, e.kind AS kind, e.props AS props, b.id AS dst "
            "ORDER BY e.kind, a.id, b.id"
        )
    finally:
        store.close()
    nodes = {r["id"]: {**r, "props": _props(r.get("props"))} for r in node_rows}
    out: dict[str, list[Edge]] = {}
    inc: dict[str, list[Edge]] = {}
    for r in edge_rows:
        edge = Edge(r["src"], r["dst"], r["kind"], _props(r.get("props")))
        out.setdefault(edge.src, []).append(edge)
        inc.setdefault(edge.dst, []).append(edge)
    return Graph(nodes, out, inc)


def _resolve_about(graph: Graph, about: str) -> str:
    q = about.strip()
    if not q:
        raise BriefError("--about must not be empty")

    if q in graph.nodes:
        return q
    if not q.startswith("route:"):
        route_id = f"route:{q}"
        if route_id in graph.nodes:
            return route_id
    file_id = q if q.startswith("file:") else f"file:{q}"
    if file_id in graph.nodes:
        return file_id

    exact = [
        n["id"] for n in graph.nodes.values()
        if q in {n.get("qname"), n.get("name")}
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        _raise_ambiguous(graph, q, exact)

    words = [w.lower() for w in q.replace("/", " ").replace("_", " ").split() if w]
    if "route" in words:
        route_term = " ".join(w for w in words if w != "route")
        exact_handler_routes = []
        route_candidates = []
        for node_id, node in graph.nodes.items():
            if node["kind"] != "Route":
                continue
            handler_names = [
                _display_name(graph.nodes[h]).replace("_", " ").lower()
                for h in _targets(graph, node_id, "HANDLED_BY")
            ]
            handler_text = " ".join(handler_names)
            if route_term and route_term in handler_names:
                exact_handler_routes.append(node_id)
            route_text = _search_text(graph, node_id).replace("_", " ")
            if route_term and (route_term in handler_text or all(w in route_text for w in route_term.split())):
                route_candidates.append(node_id)
        exact_handler_routes = sorted(set(exact_handler_routes))
        if len(exact_handler_routes) == 1:
            return exact_handler_routes[0]
        if len(exact_handler_routes) > 1:
            _raise_ambiguous(graph, q, exact_handler_routes)
        route_candidates = sorted(set(route_candidates))
        if len(route_candidates) == 1:
            return route_candidates[0]
        if route_candidates:
            _raise_ambiguous(graph, q, route_candidates)

    candidates = []
    for node_id, node in graph.nodes.items():
        if node["kind"] not in {"Route", "File", "Function", "Class", "Component"}:
            continue
        hay = _search_text(graph, node_id)
        if q.lower() in hay or (words and all(w in hay for w in words)):
            candidates.append(node_id)
    candidates = sorted(set(candidates), key=lambda nid: (_kind_rank(graph.nodes[nid]["kind"]), nid))
    if len(candidates) == 1:
        return candidates[0]
    if candidates:
        _raise_ambiguous(graph, q, candidates)
    raise BriefError(f"no graph node matched --about {q!r}")


def _render_route(target: str, graph: Graph, route_id: str) -> str:
    route = graph.nodes[route_id]
    handlers = _targets(graph, route_id, "HANDLED_BY")
    handler = handlers[0] if handlers else None
    handler_file = _file_for(graph, handler) if handler else None
    guards = _targets(graph, route_id, "GUARDED_BY")
    fetchers = _fetchers_for_route(graph, route_id)
    callees = _targets(graph, handler, "CALLS") if handler else []
    tests = _tests_for(graph, [route_id, handler, handler_file])
    file_node = graph.nodes.get(handler_file or "")
    lines = _header(target, graph, route_id, "Route")
    lines.extend([
        "",
        "## Handler",
        f"- {_fmt_node(graph, handler) if handler else '(none)'}",
        f"- Guards: {_join_nodes(graph, guards) if guards else '(none)'}",
        f"- Last commit: {_last_commit(file_node)}",
        "",
        "## Frontend Callers",
    ])
    lines.extend(_bullets(fetchers, empty="(none found through FETCHES/PROXIES)"))
    services_env = _services_env_for_file(graph, handler_file)
    lines.extend(["", "## File Touches"])
    lines.extend(_bullets(services_env, empty="(no services/env vars linked)"))
    lines.extend(["", "## Handler Callees"])
    lines.extend(_node_bullets(graph, callees[:12]))
    lines.extend(["", "## Tests"])
    lines.extend(_node_bullets(graph, tests[:12], empty="(none linked)"))
    return "\n".join(lines)


def _render_file(target: str, graph: Graph, file_id: str) -> str:
    node = graph.nodes[file_id]
    defined = _targets(graph, file_id, "DEFINES")
    routes = sorted(
        e.src for fn in defined for e in graph.inc.get(fn, [])
        if e.kind in {"HANDLED_BY", "GUARDED_BY"} and graph.nodes[e.src]["kind"] == "Route"
    )
    pages = [n for n in graph.nodes if graph.nodes[n]["kind"] == "Page" and graph.nodes[n].get("path") == node.get("path")]
    inbound = {fn: len(_sources(graph, fn, "CALLS")) for fn in defined if graph.nodes[fn]["kind"] == "Function"}
    key_symbols = sorted(inbound, key=lambda nid: (-inbound[nid], graph.nodes[nid].get("line") or 0, nid))[:10]
    importers = _sources(graph, file_id, "IMPORTS")
    imports = _targets(graph, file_id, "IMPORTS")
    tests = _tests_for(graph, [file_id, *defined])
    lines = _header(target, graph, file_id, "File")
    lines.extend([
        "",
        "## Exposed Surface",
        f"- Routes: {_join_nodes(graph, routes) if routes else '(none)'}",
        f"- Pages: {_join_nodes(graph, pages) if pages else '(none)'}",
        "",
        "## Key Symbols",
    ])
    if key_symbols:
        lines.extend(f"- {_fmt_node(graph, nid)} ({inbound[nid]} inbound CALLS)" for nid in key_symbols)
    else:
        lines.append("- (none with inbound CALLS)")
    lines.extend(["", "## Who Imports It"])
    lines.extend(_node_bullets(graph, importers[:15], empty="(none linked)"))
    lines.extend(["", "## What It Imports"])
    lines.extend(_node_bullets(graph, imports[:15], empty="(none linked)"))
    lines.extend(["", "## Services And Env"])
    lines.extend(_bullets(_services_env_for_file(graph, file_id), empty="(no services/env vars linked)"))
    lines.extend(["", "## Tests"])
    lines.extend(_node_bullets(graph, tests[:15], empty="(none linked)"))
    lines.extend(["", "## Recency", f"- Last commit: {_last_commit(node)}"])
    return "\n".join(lines)


def _render_symbol(target: str, graph: Graph, symbol_id: str) -> str:
    callers = _sources(graph, symbol_id, "CALLS")
    callees = _targets(graph, symbol_id, "CALLS")
    routes_handled = _sources(graph, symbol_id, "HANDLED_BY")
    routes_guarded = _sources(graph, symbol_id, "GUARDED_BY")
    seams = []
    for route_id in sorted(set(routes_handled + routes_guarded)):
        seams.extend(_fetchers_for_route(graph, route_id))
    tests = _tests_for(graph, [symbol_id, _file_for(graph, symbol_id)])
    similar = sorted(set(_targets(graph, symbol_id, "SIMILAR_TO") + _sources(graph, symbol_id, "SIMILAR_TO")))
    file_node = graph.nodes.get(_file_for(graph, symbol_id) or "")
    lines = _header(target, graph, symbol_id, "Symbol")
    lines.extend(["", "## Callers"])
    lines.extend(_node_bullets(graph, callers[:15], empty="(none linked)"))
    lines.extend(["", "## Callees"])
    lines.extend(_node_bullets(graph, callees[:15], empty="(none linked)"))
    lines.extend([
        "",
        "## Routes",
        f"- Handles: {_join_nodes(graph, routes_handled) if routes_handled else '(none)'}",
        f"- Guards: {_join_nodes(graph, routes_guarded) if routes_guarded else '(none)'}",
        "",
        "## Seams Reaching It",
    ])
    lines.extend(_bullets(sorted(set(seams)), empty="(none found through FETCHES/PROXIES)"))
    lines.extend(["", "## Tests"])
    lines.extend(_node_bullets(graph, tests[:15], empty="(none linked)"))
    lines.extend(["", "## Similar Symbols"])
    lines.extend(_node_bullets(graph, similar[:10], empty="(none linked; similarity extractor may not have run)"))
    lines.extend(["", "## Recency", f"- File last commit: {_last_commit(file_node)}"])
    return "\n".join(lines)


def _render_component(target: str, graph: Graph, component_id: str) -> str:
    files = [n for n in _targets(graph, component_id, "CONTAINS") if graph.nodes[n]["kind"] == "File"]
    surface = [n for n in _targets(graph, component_id, "EXPOSES") if graph.nodes[n]["kind"] in {"Route", "Page"}]
    deps = _targets(graph, component_id, "DEPENDS_ON")
    services = sorted(set(_targets(graph, component_id, "USES_SERVICE") + [
        svc for f in files for svc in _targets(graph, f, "USES_SERVICE")
    ]))
    dated = [(graph.nodes[f]["props"].get("last_commit_date") or "9999-99-99", f) for f in files]
    least_fresh = [f for _, f in sorted(dated)[:8]]
    untested = 0
    for sid in surface:
        related = [sid]
        if graph.nodes[sid]["kind"] == "Route":
            related.extend(_targets(graph, sid, "HANDLED_BY"))
        if not _tests_for(graph, related):
            untested += 1
    lines = _header(target, graph, component_id, "Component")
    lines.extend([
        "",
        "## Surface Summary",
        f"- Files: {len(files)}",
        f"- Routes/pages: {len(surface)}",
        f"- Untested surface count: {untested}",
        "",
        "## Surface",
    ])
    lines.extend(_node_bullets(graph, surface[:15], empty="(none linked)"))
    lines.extend(["", "## Direct Dependencies"])
    lines.extend(_node_bullets(graph, deps[:20], empty="(none linked)"))
    lines.extend(["", "## Services"])
    lines.extend(_node_bullets(graph, services[:15], empty="(none linked)"))
    lines.extend(["", "## Least-Fresh Files"])
    lines.extend(f"- {_fmt_node(graph, f)} last_commit={_last_commit(graph.nodes[f])}" for f in least_fresh)
    return "\n".join(lines)


def _header(target: str, graph: Graph, node_id: str, title_kind: str) -> list[str]:
    node = graph.nodes[node_id]
    where = _where(graph, node_id)
    component = _component_for(graph, node_id) or "(unknown)"
    return [
        f"# {title_kind}: {_display_name(node)}",
        f"- Defined: {where}",
        f"- Component: {component}",
        f"- FRESHNESS: {_freshness_line(target)}",
    ]


def _freshness_line(target: str) -> str:
    info = latest_snapshot_info(target)
    if not info:
        return "no snapshot found; run codeintel refresh"
    age = info.get("age_hours")
    age_text = "unknown age" if age is None else f"{int(age)}h old"
    line = f"snapshot {Path(info['path']).name} ({info['timestamp']}), {age_text}"
    if age is None or age > 24:
        line += "; run codeintel refresh"
    return line


def _fetchers_for_route(graph: Graph, route_id: str) -> list[str]:
    direct = [f"{_fmt_node(graph, e.src)} -> route" for e in graph.inc.get(route_id, []) if e.kind == "FETCHES"]
    proxied = []
    for e in graph.inc.get(route_id, []):
        if e.kind != "PROXIES":
            continue
        proxy = e.src
        for fe in graph.inc.get(proxy, []):
            if fe.kind == "FETCHES":
                proxied.append(f"{_fmt_node(graph, fe.src)} -> {_fmt_node(graph, proxy)}")
    return sorted(set(direct + proxied))


def _services_env_for_file(graph: Graph, file_id: str | None) -> list[str]:
    if not file_id:
        return []
    values = []
    for e in graph.out.get(file_id, []):
        if e.kind == "USES_SERVICE":
            values.append(f"service: {_fmt_node(graph, e.dst)}")
        elif e.kind == "READS_ENV":
            values.append(f"env: {_fmt_node(graph, e.dst)}")
    return sorted(values)


def _tests_for(graph: Graph, ids: list[str | None]) -> list[str]:
    found = []
    for node_id in ids:
        if not node_id:
            continue
        found.extend(e.dst for e in graph.out.get(node_id, []) if e.kind == "TESTED_BY")
    return sorted(set(found))


def _file_for(graph: Graph, node_id: str | None) -> str | None:
    if not node_id or node_id not in graph.nodes:
        return None
    node = graph.nodes[node_id]
    if node["kind"] == "File":
        return node_id
    path = node.get("path")
    if path and f"file:{path}" in graph.nodes:
        return f"file:{path}"
    return None


def _component_for(graph: Graph, node_id: str) -> str | None:
    file_id = _file_for(graph, node_id)
    node = graph.nodes[node_id]
    if node["kind"] == "Component":
        return node.get("name") or node["id"].removeprefix("component:")
    if file_id:
        file_node = graph.nodes[file_id]
        if file_node["props"].get("component"):
            return file_node["props"]["component"]
        for e in graph.inc.get(file_id, []):
            if e.kind == "CONTAINS" and graph.nodes[e.src]["kind"] == "Component":
                return graph.nodes[e.src].get("name") or e.src.removeprefix("component:")
    for e in graph.inc.get(node_id, []):
        if graph.nodes[e.src]["kind"] == "Component":
            return graph.nodes[e.src].get("name") or e.src.removeprefix("component:")
    return None


def _where(graph: Graph, node_id: str) -> str:
    node = graph.nodes[node_id]
    if node["kind"] == "Route":
        handlers = _targets(graph, node_id, "HANDLED_BY")
        if handlers:
            return _where(graph, handlers[0])
        return node["id"]
    path = node.get("path") or node["props"].get("path")
    line = node.get("line") or node["props"].get("line")
    if path and line:
        return f"{path}:{line}"
    if path:
        return path
    return node["id"]


def _targets(graph: Graph, node_id: str | None, kind: str) -> list[str]:
    if not node_id:
        return []
    return sorted(set(e.dst for e in graph.out.get(node_id, []) if e.kind == kind))


def _sources(graph: Graph, node_id: str | None, kind: str) -> list[str]:
    if not node_id:
        return []
    return sorted(set(e.src for e in graph.inc.get(node_id, []) if e.kind == kind))


def _node_bullets(graph: Graph, ids: list[str], empty: str = "(none)") -> list[str]:
    if not ids:
        return [f"- {empty}"]
    return [f"- {_fmt_node(graph, nid)}" for nid in ids]


def _bullets(values: list[str], empty: str = "(none)") -> list[str]:
    if not values:
        return [f"- {empty}"]
    return [f"- {v}" for v in values]


def _join_nodes(graph: Graph, ids: list[str]) -> str:
    return ", ".join(_fmt_node(graph, nid) for nid in ids)


def _fmt_node(graph: Graph, node_id: str | None) -> str:
    if not node_id or node_id not in graph.nodes:
        return "(unknown)"
    node = graph.nodes[node_id]
    label = _display_name(node)
    where = _where(graph, node_id)
    if where and where != node_id:
        return f"{label} ({where})"
    return label


def _display_name(node: dict[str, Any]) -> str:
    if node.get("qname"):
        return node["qname"]
    if node.get("name"):
        return node["name"]
    return node["id"].split(":", 1)[-1]


def _last_commit(node: dict[str, Any] | None) -> str:
    if not node:
        return "(unknown)"
    return str(node.get("props", {}).get("last_commit_date") or "(unknown)")


def _search_text(graph: Graph, node_id: str) -> str:
    node = graph.nodes[node_id]
    bits = [node_id, node.get("name"), node.get("qname"), node.get("path"), json.dumps(node.get("props", {}), sort_keys=True)]
    if node["kind"] == "Route":
        bits.extend(_display_name(graph.nodes[h]) for h in _targets(graph, node_id, "HANDLED_BY"))
        bits.extend(_display_name(graph.nodes[g]) for g in _targets(graph, node_id, "GUARDED_BY"))
    return " ".join(str(b) for b in bits if b).lower()


def _raise_ambiguous(graph: Graph, about: str, candidates: list[str]) -> None:
    lines = [f"ambiguous --about {about!r}; candidates:"]
    for node_id in candidates[:30]:
        node = graph.nodes[node_id]
        lines.append(f"- {node['kind']}: {_fmt_node(graph, node_id)} [{node_id}]")
    if len(candidates) > 30:
        lines.append(f"- ... {len(candidates) - 30} more")
    raise BriefError("\n".join(lines), code=3)


def _kind_rank(kind: str) -> int:
    return {"Route": 0, "File": 1, "Function": 2, "Class": 3, "Component": 4}.get(kind, 9)


def _props(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}
