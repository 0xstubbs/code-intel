"""Graph-backed code-health findings report."""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from codeintel import OUT_DIR, TARGETS_DIR
from codeintel.config import ConfigError, TargetConfig, load_target_by_name
from codeintel.core.graph import KuzuGraphStore
from codeintel.monitor import latest_snapshot_info


SEVERITIES = ("info", "review", "action")
CONFIDENCES = ("high", "medium", "low")
MAX_MD_ITEMS = 15
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
TEST_PATH_MARKERS = ("/test/", "/tests/", "/__tests__/", ".test.", ".spec.", "_test.")
SCRIPT_PATH_MARKERS = ("/scripts/", "/script/", "/e2e/", "/fixtures/", "/migrations/")
ENTRY_NAMES = {
    "main",
    "__main__",
    "cli",
    "cli_run",
    "run",
    "handler",
    "lifespan",
    "startup",
    "shutdown",
}


class FindingsError(Exception):
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
    try:
        out_dir = write_findings(target)
    except FindingsError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code
    print(f"findings: {out_dir / 'FINDINGS.md'}")
    print(f"json: {out_dir / 'findings.json'}")
    return 0


def write_findings(target: TargetConfig) -> Path:
    db = OUT_DIR / target.name / "graph.kuzu"
    if not db.exists():
        raise FindingsError(f"no graph at {db}; run `codeintel index {target.name}` first")
    graph = _load_graph(db)
    findings = build_findings(target, graph)
    out_dir = OUT_DIR / target.name / "findings"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "findings.json").write_text(
        json.dumps(findings, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (out_dir / "FINDINGS.md").write_text(
        render_markdown(target, graph, findings),
        encoding="utf-8",
    )
    return out_dir


def build_findings(target: TargetConfig, graph: Graph) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    builders = [
        _dead_code_candidates,
        _stale_but_wired,
        _untested_surface,
        _unguarded_mutating_routes,
        _duplication_clusters,
        _production_orphaned_modules,
        _env_config_surface,
    ]
    for builder in builders:
        items.extend(builder(target, graph))
    for idx, item in enumerate(items, start=1):
        item["id"] = f"F{idx:04d}"
    return items


def render_markdown(target: TargetConfig, graph: Graph, findings: list[dict[str, Any]]) -> str:
    snapshot = latest_snapshot_info(target.name)
    snapshot_ts = snapshot["timestamp"] if snapshot else "unknown"
    lines = [
        f"# Code Health Findings: {target.name}",
        "",
        "## Preamble",
        f"- Graph snapshot timestamp: {snapshot_ts}",
        "- Scope: generic graph-only analysis. It does not read application semantics beyond extracted nodes, edges, and node properties.",
        "- Known limits: dynamic calls, reflection, framework registries, template references, external API consumers, and unresolvable imports can be missed by the extractors.",
        "- Severity is a triage hint. `review` means human intent matters; `action` means the graph shows a concrete risk worth prioritizing.",
        "",
        "## Summary",
        "",
        "| Category | Info | Review | Action | Total |",
        "|---|---:|---:|---:|---:|",
    ]
    for category in sorted({f["category"] for f in findings}):
        bucket = [f for f in findings if f["category"] == category]
        counts = {sev: sum(1 for f in bucket if f["severity"] == sev) for sev in SEVERITIES}
        lines.append(
            f"| {category} | {counts['info']} | {counts['review']} | {counts['action']} | {len(bucket)} |"
        )
    if not findings:
        lines.append("| none | 0 | 0 | 0 | 0 |")

    lines.extend(["", "## Services Per Component", ""])
    svc_rows = _services_by_component(graph)
    if svc_rows:
        lines.extend(["| Component | Services |", "|---|---:|"])
        for component, count in svc_rows:
            lines.append(f"| {component} | {count} |")
    else:
        lines.append("(no service usage edges)")

    lines.extend([""])
    for category in sorted({f["category"] for f in findings}):
        bucket = [f for f in findings if f["category"] == category]
        lines.extend([f"## {category}", ""])
        lines.extend(_section_intro(category))
        for item in bucket[:MAX_MD_ITEMS]:
            lines.append(
                f"- **{item['severity']} / {item['confidence']}** `{item['id']}` {item['title']}"
            )
            lines.append(f"  - {item['detail']}")
            evidence = ", ".join(item.get("evidence", [])[:6])
            if evidence:
                lines.append(f"  - Evidence: {evidence}")
        more = len(bucket) - MAX_MD_ITEMS
        if more > 0:
            lines.append(f"- and {more} more in `findings.json`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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


def _dead_code_candidates(target: TargetConfig, graph: Graph) -> list[dict[str, Any]]:
    role_nodes = _role_nodes(graph)
    page_paths = {n.get("path") for n in graph.nodes.values() if n["kind"] == "Page" and n.get("path")}
    rows = []
    for node_id, node in graph.nodes.items():
        if node["kind"] != "Function":
            continue
        path = str(node.get("path") or "")
        name = str(node.get("name") or "")
        if node_id in role_nodes or path in page_paths or _is_test_path(path) or _is_script_path(path):
            continue
        if path.endswith("__init__.py") or _is_entry_name(name):
            continue
        if _is_direct_class_method(graph, node_id):
            continue
        if _is_nested_local_function(graph, node_id):
            continue
        usage_inbound = [e for e in graph.inc.get(node_id, []) if e.kind != "DEFINES"]
        if usage_inbound:
            continue
        confidence = "high" if _is_high_confidence_dead_candidate(graph, node_id) else "medium"
        severity = "action" if confidence == "high" else "review"
        rows.append((confidence, path, node.get("line") or 0, node_id, node, severity))
    rows.sort(key=lambda r: (0 if r[0] == "high" else 1, r[1], r[2], r[3]))
    return [
        _finding(
            "dead-code-candidates",
            severity,
            confidence,
            f"{_display_node(node)} has no graph usage inbound",
            "No inbound non-structural edges were found. Dynamic dispatch, method dispatch, first-class function references, and external callers may be invisible to the graph.",
            [_evidence(node_id, node)],
        )
        for confidence, _path, _line, node_id, node, severity in rows
    ]


def _stale_but_wired(target: TargetConfig, graph: Graph) -> list[dict[str, Any]]:
    dated = []
    for node_id, node in graph.nodes.items():
        if node["kind"] != "File":
            continue
        d = _date_prop(node)
        if d is not None:
            dated.append((d, node_id, node))
    if not dated:
        return []
    ords = sorted(d.toordinal() for d, _node_id, _node in dated)
    median_ord = int(statistics.median(ords))
    rows = []
    for d, node_id, node in dated:
        if d.toordinal() >= median_ord or _is_test_path(str(node.get("path") or "")):
            continue
        inbound = _file_inbound_usage(graph, node_id)
        if inbound < 2:
            continue
        age = median_ord - d.toordinal()
        rows.append((age * inbound, age, inbound, node_id, node))
    rows.sort(key=lambda r: (-r[0], r[4].get("path") or "", r[3]))
    return [
        _finding(
            "stale-but-wired",
            "review",
            "medium",
            f"{node.get('path')} is older than median but still wired",
            f"Last commit date {node['props'].get('last_commit_date')} is {age} days older than median; inbound usage degree is {inbound}.",
            [_evidence(node_id, node)],
        )
        for _score, age, inbound, node_id, node in rows[:10]
    ]


def _untested_surface(target: TargetConfig, graph: Graph) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    component_counts: dict[str, dict[str, int]] = {}
    for route_id, route in graph.nodes.items():
        if route["kind"] != "Route":
            continue
        component = _exposing_component(graph, route_id) or "unknown"
        component_counts.setdefault(component, {"total": 0, "missing": 0, "weak": 0})
        component_counts[component]["total"] += 1
        handlers = _targets(graph, route_id, "HANDLED_BY")
        handler_id = handlers[0] if handlers else None
        if handler_id is None:
            continue
        handler = graph.nodes[handler_id]
        function_tests = _tests_for_node(graph, handler_id, "function")
        weak_tests = _weak_tests_for_handler(graph, handler_id)
        if function_tests:
            continue
        if weak_tests:
            component_counts[component]["weak"] += 1
            continue
        component_counts[component]["missing"] += 1
        rows.append(
            _finding(
                "untested-surface",
                "action",
                "medium",
                f"{route_id.removeprefix('route:')} has no linked handler tests",
                "The route handler has no function-level or file-level TESTED_BY linkage.",
                [_evidence(route_id, route), _evidence(handler_id, handler)],
            )
        )
    for component, counts in sorted(component_counts.items()):
        missing = counts["missing"]
        weak = counts["weak"]
        if missing == 0 and weak == 0:
            continue
        rows.append(
            _finding(
                "untested-surface",
                "review" if missing else "info",
                "medium",
                f"{component} exposes {missing} untested routes and {weak} weakly covered routes",
                "Function-level TESTED_BY linkage is strongest; file-level linkage is counted as weak coverage.",
                [f"component:{component}"],
            )
        )
    return sorted(rows, key=lambda f: (f["severity"], f["title"], f["evidence"]))


def _unguarded_mutating_routes(target: TargetConfig, graph: Graph) -> list[dict[str, Any]]:
    rows = []
    for route_id, route in graph.nodes.items():
        if route["kind"] != "Route":
            continue
        method = str(route["props"].get("method") or route_id.split(":", 1)[-1].split(" ", 1)[0]).upper()
        if method not in MUTATING_METHODS:
            continue
        if _targets(graph, route_id, "GUARDED_BY"):
            continue
        rows.append((method, route_id, route))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [
        _finding(
            "unguarded-mutating-routes",
            "review",
            "medium",
            f"{route_id.removeprefix('route:')} has no guard edge",
            "No GUARDED_BY edge is linked. The graph cannot determine whether this route intentionally allows unauthenticated mutation.",
            [_evidence(route_id, route)],
        )
        for _method, route_id, route in rows
    ]


def _duplication_clusters(target: TargetConfig, graph: Graph) -> list[dict[str, Any]]:
    parent: dict[str, str] = {}
    same_file_pairs = 0
    pair_props: dict[tuple[str, str], dict[str, Any]] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for edges in graph.out.values():
        for edge in edges:
            if edge.kind != "SIMILAR_TO":
                continue
            score = float(edge.props.get("score") or 0)
            method = str(edge.props.get("method") or "")
            if score < 0.9 and method != "exact":
                continue
            a = graph.nodes.get(edge.src)
            b = graph.nodes.get(edge.dst)
            if not a or not b:
                continue
            if a.get("path") == b.get("path"):
                same_file_pairs += 1
                continue
            union(edge.src, edge.dst)
            pair_props[tuple(sorted((edge.src, edge.dst)))] = edge.props

    clusters: dict[str, list[str]] = {}
    for node_id in sorted(parent):
        clusters.setdefault(find(node_id), []).append(node_id)

    rows = []
    for members in clusters.values():
        if len(members) < 2:
            continue
        files = sorted({str(graph.nodes[m].get("path") or "") for m in members})
        loc = sum(
            int(pair_props.get(tuple(sorted((a, b))), {}).get("loc") or 0)
            for i, a in enumerate(members)
            for b in members[i + 1 :]
        )
        rows.append((len(members), loc, files, members))
    rows.sort(key=lambda r: (-r[0], -r[1], r[2], r[3]))
    findings = []
    for size, loc, files, members in rows:
        nodes = [graph.nodes[m] for m in members]
        title_files = ", ".join(files[:3])
        if len(files) > 3:
            title_files += f", +{len(files) - 3} files"
        findings.append(
            _finding(
                "duplication-clusters",
                "review",
                "high",
                f"{size} similar functions across {len(files)} files",
                f"Approximate duplicated LOC from qualifying edges: {loc}. Consider extracting shared behavior or deleting stale parallel implementations. Files: {title_files}. Same-file qualifying pairs excluded from clusters: {same_file_pairs}.",
                [_evidence(m, n) for m, n in zip(members, nodes)],
            )
        )
    return findings


def _production_orphaned_modules(target: TargetConfig, graph: Graph) -> list[dict[str, Any]]:
    rows = []
    for node_id, node in graph.nodes.items():
        if node["kind"] != "File":
            continue
        path = str(node.get("path") or "")
        if _is_test_path(path):
            continue
        importers = [e.src for e in graph.inc.get(node_id, []) if e.kind == "IMPORTS" and _is_file(graph, e.src)]
        if not importers:
            continue
        if all(_is_test_path(str(graph.nodes[src].get("path") or "")) for src in importers):
            rows.append((path, node_id, node, importers))
    rows.sort(key=lambda r: (r[0], r[1]))
    return [
        _finding(
            "production-orphaned-modules",
            "review",
            "medium",
            f"{path} is imported only by tests",
            f"{len(importers)} importer(s), all test files. Production import graph does not reach this module.",
            [_evidence(node_id, node)] + [_evidence(src, graph.nodes[src]) for src in importers[:5]],
        )
        for path, node_id, node, importers in rows
    ]


def _env_config_surface(target: TargetConfig, graph: Graph) -> list[dict[str, Any]]:
    rows = []
    for env_id, env in graph.nodes.items():
        if env["kind"] != "EnvVar":
            continue
        readers = [e.src for e in graph.inc.get(env_id, []) if e.kind == "READS_ENV" and _is_file(graph, e.src)]
        readers = sorted(set(readers))
        if len(readers) != 1:
            continue
        file_id = readers[0]
        if _file_has_any_tests(graph, file_id):
            continue
        file_node = graph.nodes[file_id]
        rows.append((env.get("name") or env_id, env_id, env, file_id, file_node))
    rows.sort(key=lambda r: (str(r[0]), str(r[4].get("path") or ""), r[1]))
    findings = [
        _finding(
            "env-config-surface",
            "review",
            "medium",
            f"{name} is read in one untested file",
            f"Only {_display_node(file_node)} reads this env var, and that file has no TESTED_BY linkage.",
            [_evidence(env_id, env), _evidence(file_id, file_node)],
        )
        for name, env_id, env, file_id, file_node in rows
    ]
    for component, count in _services_by_component(graph):
        findings.append(
            _finding(
                "env-config-surface",
                "info",
                "high",
                f"{component} touches {count} service(s)",
                "Service count from USES_SERVICE edges on component files.",
                [f"component:{component}"],
            )
        )
    return findings


def _finding(
    category: str,
    severity: str,
    confidence: str,
    title: str,
    detail: str,
    evidence: list[str],
) -> dict[str, Any]:
    return {
        "id": "",
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "title": title,
        "detail": detail,
        "evidence": sorted(str(e) for e in evidence if e),
    }


def _section_intro(category: str) -> list[str]:
    if category == "dead-code-candidates":
        return ["Candidates have no inbound usage edges after excluding framework roles, tests, scripts, page files, class methods, entry points, and `__init__.py` exports.", ""]
    if category == "untested-surface":
        return ["File-level TESTED_BY linkage is weak coverage; these items focus on missing function-level route coverage and component exposure.", ""]
    if category == "unguarded-mutating-routes":
        return ["These are review items because the graph cannot know whether auth is required for a route.", ""]
    return []


def _props(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _role_nodes(graph: Graph) -> set[str]:
    out = set()
    for edges in graph.out.values():
        for e in edges:
            if e.kind in {"HANDLED_BY", "GUARDED_BY"}:
                out.add(e.dst)
    return out


def _targets(graph: Graph, node_id: str, kind: str) -> list[str]:
    return sorted(e.dst for e in graph.out.get(node_id, []) if e.kind == kind)


def _sources(graph: Graph, node_id: str, kind: str) -> list[str]:
    return sorted(e.src for e in graph.inc.get(node_id, []) if e.kind == kind)


def _tests_for_node(graph: Graph, node_id: str, linkage: str | None = None) -> list[str]:
    out = []
    for e in graph.out.get(node_id, []):
        if e.kind != "TESTED_BY":
            continue
        if linkage is not None and e.props.get("linkage") != linkage:
            continue
        out.append(e.dst)
    return sorted(out)


def _weak_tests_for_handler(graph: Graph, handler_id: str) -> list[str]:
    tests = _tests_for_node(graph, handler_id, "file")
    file_id = _file_for(graph, handler_id)
    if file_id:
        tests.extend(_tests_for_node(graph, file_id, "file"))
    return sorted(set(tests))


def _file_has_any_tests(graph: Graph, file_id: str) -> bool:
    if _tests_for_node(graph, file_id):
        return True
    for e in graph.out.get(file_id, []):
        if e.kind == "DEFINES" and _tests_for_node(graph, e.dst):
            return True
    return False


def _file_for(graph: Graph, node_id: str) -> str | None:
    node = graph.nodes.get(node_id)
    if not node:
        return None
    path = node.get("path")
    file_id = f"file:{path}" if path else None
    return file_id if file_id in graph.nodes else None


def _file_inbound_usage(graph: Graph, file_id: str) -> int:
    degree = len([e for e in graph.inc.get(file_id, []) if e.kind == "IMPORTS" and _is_file(graph, e.src)])
    for edge in graph.out.get(file_id, []):
        if edge.kind != "DEFINES":
            continue
        degree += len([e for e in graph.inc.get(edge.dst, []) if e.kind in {"CALLS", "HANDLED_BY", "GUARDED_BY", "FETCHES"}])
    return degree


def _exposing_component(graph: Graph, node_id: str) -> str | None:
    for src in _sources(graph, node_id, "EXPOSES"):
        node = graph.nodes.get(src)
        if node and node["kind"] == "Component":
            return str(node.get("name") or src.removeprefix("component:"))
    return None


def _services_by_component(graph: Graph) -> list[tuple[str, int]]:
    component_files: dict[str, set[str]] = {}
    for src, edges in graph.out.items():
        node = graph.nodes.get(src)
        if not node or node["kind"] != "Component":
            continue
        component = str(node.get("name") or src.removeprefix("component:"))
        component_files.setdefault(component, set())
        for e in edges:
            if e.kind == "CONTAINS" and _is_file(graph, e.dst):
                component_files[component].add(e.dst)

    rows = []
    for component, files in sorted(component_files.items()):
        flat: set[str] = set()
        for file_id in files:
            flat.update(_targets(graph, file_id, "USES_SERVICE"))
        component_node = f"component:{component}"
        flat.update(_targets(graph, component_node, "USES_SERVICE"))
        if flat:
            rows.append((component, len(flat)))
    return rows


def _date_prop(node: dict[str, Any]) -> date | None:
    raw = node["props"].get("last_commit_date")
    if not raw:
        return None
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


def _is_file(graph: Graph, node_id: str) -> bool:
    node = graph.nodes.get(node_id)
    return bool(node and node["kind"] == "File")


def _is_test_path(path: str) -> bool:
    p = "/" + path.lower()
    name = Path(path).name.lower()
    return any(marker in p for marker in TEST_PATH_MARKERS) or name.startswith("test_") or name.endswith("_test.py")


def _is_script_path(path: str) -> bool:
    p = "/" + path.lower()
    return any(marker in p for marker in SCRIPT_PATH_MARKERS)


def _is_entry_name(name: str) -> bool:
    if name in ENTRY_NAMES:
        return True
    if name.endswith("_middleware"):
        return True
    return name.startswith("__") and name.endswith("__")


def _is_high_confidence_dead_candidate(graph: Graph, node_id: str) -> bool:
    node = graph.nodes[node_id]
    name = str(node.get("name") or "")
    path = str(node.get("path") or "")
    if not name.startswith("_") or name.startswith("__"):
        return False
    if path.endswith(".py"):
        # Python method dispatch and first-class references are common static blind spots.
        return False
    return True


def _is_direct_class_method(graph: Graph, node_id: str) -> bool:
    qname = str(graph.nodes[node_id].get("qname") or "")
    if "." not in qname:
        return False
    parent_qname = qname.rsplit(".", 1)[0]
    for edge in graph.inc.get(node_id, []):
        if edge.kind != "DEFINES":
            continue
        parent = graph.nodes.get(edge.src)
        if parent and parent["kind"] == "Class" and parent.get("qname") == parent_qname:
            return True
    return False


def _is_nested_local_function(graph: Graph, node_id: str) -> bool:
    node = graph.nodes[node_id]
    qname = str(node.get("qname") or "")
    if "." not in qname:
        return False
    parent_qname = qname.rsplit(".", 1)[0]
    for edge in graph.inc.get(node_id, []):
        if edge.kind != "DEFINES":
            continue
        parent = graph.nodes.get(edge.src)
        if parent and parent["kind"] == "Class" and parent.get("qname") == parent_qname:
            return False
    return True


def _display_node(node: dict[str, Any]) -> str:
    path = node.get("path") or node.get("id")
    line = node.get("line")
    suffix = f":{line}" if line else ""
    name = node.get("qname") or node.get("name") or node.get("id")
    return f"{path}{suffix} ({name})"


def _evidence(node_id: str, node: dict[str, Any]) -> str:
    path = node.get("path")
    line = node.get("line")
    if path:
        loc = f"{path}:{line}" if line else str(path)
        return f"{loc} [{node_id}]"
    return node_id


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    return cli_run(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
