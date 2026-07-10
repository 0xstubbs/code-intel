"""Architecture map generator for `codeintel map <target>`.

The generator is intentionally fact-driven: it reads the extractor JSON
interchange files and applies the same "later facts patch node props" merge
rule as the graph loader. No target source tree is read.
"""

from __future__ import annotations

import html
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codeintel import OUT_DIR, TARGETS_DIR
from codeintel.config import ConfigError, Component, TargetConfig, load_target_by_name


@dataclass(frozen=True)
class Graph:
    nodes: dict[str, dict[str, Any]]
    edges: list[dict[str, Any]]


def cli_run(args) -> int:
    try:
        target = load_target_by_name(args.target, TARGETS_DIR)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    facts_dir = OUT_DIR / target.name / "facts"
    if not facts_dir.is_dir():
        print(f"error: no facts at {facts_dir}; run `codeintel index {target.name}` first",
              file=sys.stderr)
        return 1
    try:
        graph = load_facts(facts_dir)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    out_dir = OUT_DIR / target.name / "map"
    out_dir.mkdir(parents=True, exist_ok=True)
    model = build_model(target, graph)
    md = render_markdown(target, graph, model)
    viz = render_html(target, graph, model)
    (out_dir / "ARCHITECTURE-MAP.md").write_text(md, encoding="utf-8")
    (out_dir / "viz.html").write_text(viz, encoding="utf-8")
    print(f"wrote {out_dir / 'ARCHITECTURE-MAP.md'}")
    print(f"wrote {out_dir / 'viz.html'}")
    return 0


def load_facts(facts_dir: Path) -> Graph:
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[dict[str, Any]] = []
    files = sorted(facts_dir.glob("*.json"))
    if not files:
        raise ValueError(f"no fact files found under {facts_dir}")
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for node in doc.get("nodes", []):
            nid = str(node["id"])
            prev = nodes.get(nid)
            props = dict(node.get("props", {}))
            if prev is None:
                nodes[nid] = {"id": nid, "kind": str(node["kind"]), "props": props}
            else:
                prev["props"].update(props)
        for edge in doc.get("edges", []):
            edges.append({
                "src": str(edge["src"]),
                "dst": str(edge["dst"]),
                "kind": str(edge["kind"]),
                "props": dict(edge.get("props", {})),
            })
    edges = [e for e in edges if e["src"] in nodes and e["dst"] in nodes]
    edges.sort(key=lambda e: (e["kind"], e["src"], e["dst"], stable_json(e["props"])))
    return Graph(nodes=dict(sorted(nodes.items())), edges=edges)


def build_model(target: TargetConfig, graph: Graph) -> dict[str, Any]:
    components = sorted(target.components, key=lambda c: c.name)
    comp_by_name = {c.name: c for c in components}
    comp_for_id = {f"component:{c.name}": c.name for c in components}
    for nid, node in graph.nodes.items():
        comp = component_for_node(target, nid, node)
        if comp:
            comp_for_id[nid] = comp

    out_edges = defaultdict(list)
    in_edges = defaultdict(list)
    for edge in graph.edges:
        out_edges[edge["src"]].append(edge)
        in_edges[edge["dst"]].append(edge)

    children = defaultdict(list)
    for edge in graph.edges:
        if edge["kind"] in {"CONTAINS", "DEFINES"}:
            children[edge["src"]].append(edge["dst"])

    file_tests = Counter()
    kept_edges_for_viz = []
    for edge in graph.edges:
        if edge["kind"] == "TESTED_BY" and graph.nodes[edge["src"]]["kind"] == "File":
            file_tests[edge["src"]] += 1
        else:
            kept_edges_for_viz.append(edge)

    comps: dict[str, dict[str, Any]] = {}
    for comp in components:
        nodes = [nid for nid, n in graph.nodes.items()
                 if comp_for_id.get(nid) == comp.name and n["kind"] != "Component"]
        files = [nid for nid in nodes if graph.nodes[nid]["kind"] == "File"]
        funcs = [nid for nid in nodes if graph.nodes[nid]["kind"] == "Function"]
        classes = [nid for nid in nodes if graph.nodes[nid]["kind"] == "Class"]
        groups = defaultdict(lambda: Counter({"files": 0, "functions": 0, "classes": 0}))
        for fid in files:
            group = module_group(comp, graph.nodes[fid]["props"].get("path", ""))
            groups[group]["files"] += 1
        for nid in funcs:
            groups[module_group(comp, graph.nodes[nid]["props"].get("path", ""))]["functions"] += 1
        for nid in classes:
            groups[module_group(comp, graph.nodes[nid]["props"].get("path", ""))]["classes"] += 1
        comps[comp.name] = {
            "config": comp,
            "nodes": nodes,
            "files": files,
            "functions": funcs,
            "classes": classes,
            "groups": dict(sorted(groups.items())),
        }

    exposes = defaultdict(list)
    deps = defaultdict(list)
    for edge in graph.edges:
        if edge["kind"] == "EXPOSES" and edge["src"].startswith("component:"):
            exposes[edge["src"].removeprefix("component:")].append(edge["dst"])
        if edge["kind"] == "DEPENDS_ON" and edge["src"].startswith("component:"):
            deps[edge["src"].removeprefix("component:")].append((edge["dst"], edge["props"]))

    services = defaultdict(set)
    envs = defaultdict(set)
    for edge in graph.edges:
        if edge["kind"] not in {"USES_SERVICE", "READS_ENV"}:
            continue
        comp = comp_for_id.get(edge["src"])
        if not comp:
            continue
        label = node_label(graph, edge["dst"])
        (services if edge["kind"] == "USES_SERVICE" else envs)[comp].add(label)

    route_handlers = defaultdict(list)
    route_guards = defaultdict(list)
    proxy_routes = defaultdict(list)
    fetches = []
    for edge in graph.edges:
        if edge["kind"] == "HANDLED_BY":
            route_handlers[edge["src"]].append(edge["dst"])
        elif edge["kind"] == "GUARDED_BY":
            route_guards[edge["src"]].append(edge["dst"])
        elif edge["kind"] == "PROXIES":
            proxy_routes[edge["src"]].append(edge["dst"])
        elif edge["kind"] == "FETCHES":
            fetches.append(edge)

    seams = []
    for edge in fetches:
        dsts = proxy_routes.get(edge["dst"], [edge["dst"]])
        for route in dsts:
            for handler in route_handlers.get(route, [""]):
                seams.append({
                    "frontend": edge["src"],
                    "frontend_label": symbol_label(graph, edge["src"]),
                    "fetch_method": edge["props"].get("method", ""),
                    "proxy": edge["dst"] if graph.nodes[edge["dst"]]["kind"] == "ApiProxy" else "",
                    "proxy_label": node_label(graph, edge["dst"]),
                    "route": route,
                    "route_label": route_label(graph, route),
                    "handler": handler,
                    "handler_label": symbol_label(graph, handler) if handler else "",
                })
    seams.sort(key=lambda s: (s["frontend_label"], s["route_label"], s["handler_label"]))

    tested_by_kind = Counter()
    tested_nodes = defaultdict(set)
    for edge in graph.edges:
        if edge["kind"] == "TESTED_BY":
            kind = graph.nodes[edge["src"]]["kind"]
            tested_by_kind[kind] += 1
            tested_nodes[kind].add(edge["src"])

    least_tested = []
    for name, data in comps.items():
        testable_nodes = data["files"] + data["functions"] + data["classes"]
        testable_count = len(testable_nodes)
        covered = len(set(testable_nodes) &
                      set().union(*tested_nodes.values()) if tested_nodes else set())
        pct = 0.0 if not testable_count else covered / testable_count * 100
        least_tested.append((pct, covered, testable_count, name))
    least_tested.sort(key=lambda x: (x[0], x[3]))

    recency = {}
    for name, data in comps.items():
        dated = [(graph.nodes[f]["props"].get("last_commit_date"), f)
                 for f in data["files"] if graph.nodes[f]["props"].get("last_commit_date")]
        dated.sort()
        recency[name] = {"oldest": dated[:1], "newest": dated[-1:] if dated else []}

    viz_nodes, viz_edges = build_viz_data(target, graph, comp_for_id, children,
                                          file_tests, kept_edges_for_viz)
    return {
        "components": comps,
        "comp_by_name": comp_by_name,
        "comp_for_id": comp_for_id,
        "out_edges": out_edges,
        "in_edges": in_edges,
        "exposes": exposes,
        "deps": deps,
        "services": services,
        "envs": envs,
        "route_handlers": route_handlers,
        "route_guards": route_guards,
        "seams": seams,
        "tested_by_kind": tested_by_kind,
        "least_tested": least_tested,
        "recency": recency,
        "viz": {"nodes": viz_nodes, "edges": viz_edges, "seams": seams},
        "file_test_edges_aggregated": sum(file_tests.values()),
    }


def build_viz_data(target: TargetConfig, graph: Graph, comp_for_id: dict[str, str],
                   children: dict[str, list[str]], file_tests: Counter,
                   raw_edges: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    nodes = []
    seen_groups = set()
    for nid, node in graph.nodes.items():
        if node["kind"] == "Target":
            props = node["props"]
            nodes.append({
                "id": nid, "kind": "Target", "label": node_label(graph, nid),
                "name": props.get("name", ""), "path": props.get("path", ""),
                "component": "", "group": "", "parent": "",
                "props": dict(sorted(props.items())),
            })
    for comp in sorted(target.components, key=lambda c: c.name):
        nodes.append({
            "id": f"component:{comp.name}", "kind": "Component", "label": comp.name,
            "name": comp.name, "path": comp.path, "component": comp.name, "group": "",
            "parent": "", "props": {"lang": comp.lang, "framework": comp.framework or "", "path": comp.path},
        })
    for nid, node in graph.nodes.items():
        if node["kind"] in {"Target", "Component"}:
            continue
        props = node["props"]
        comp = comp_for_id.get(nid, "")
        path = props.get("path", "")
        group = module_group_name(target, comp, path)
        if comp and group:
            gid = f"group:{comp}:{group}"
            if gid not in seen_groups:
                seen_groups.add(gid)
                nodes.append({"id": gid, "kind": "ModuleGroup", "label": group, "name": group,
                              "path": group, "component": comp, "group": group,
                              "parent": f"component:{comp}", "props": {"path": group}})
        parent = parent_for_node(target, graph, nid, node, comp, group, children)
        entry = {
            "id": nid,
            "kind": node["kind"],
            "label": node_label(graph, nid),
            "name": props.get("name", ""),
            "qname": props.get("qname", ""),
            "path": path,
            "component": comp,
            "group": group,
            "parent": parent,
            "props": dict(sorted(props.items())),
        }
        if file_tests[nid]:
            entry["props"]["aggregated_file_tested_by"] = file_tests[nid]
        nodes.append(entry)
    node_ids = {n["id"] for n in nodes}
    edges = []
    for edge in raw_edges:
        if edge["src"] in node_ids and edge["dst"] in node_ids:
            edges.append({"src": edge["src"], "dst": edge["dst"], "kind": edge["kind"],
                          "props": dict(sorted(edge["props"].items()))})
    return sorted(nodes, key=lambda n: (n["kind"], n["component"], n["path"], n["id"])), edges


def parent_for_node(target: TargetConfig, graph: Graph, nid: str, node: dict[str, Any],
                    comp: str, group: str, children: dict[str, list[str]]) -> str:
    kind = node["kind"]
    if kind in {"Route", "Page", "ApiProxy", "Dependency", "Service", "EnvVar", "Test"}:
        return f"component:{comp}" if comp else ""
    if kind == "File":
        return f"group:{comp}:{group}" if comp and group else (f"component:{comp}" if comp else "")
    path = node["props"].get("path", "")
    fid = f"file:{path}" if path else ""
    if fid in graph.nodes:
        return fid
    return f"group:{comp}:{group}" if comp and group else (f"component:{comp}" if comp else "")


def render_markdown(target: TargetConfig, graph: Graph, model: dict[str, Any]) -> str:
    node_counts = Counter(n["kind"] for n in graph.nodes.values())
    edge_counts = Counter(e["kind"] for e in graph.edges)
    lines = [
        f"# Architecture Map: {target.name}",
        "",
        "Generated from codeintel graph facts. Output is deterministic and contains no generation timestamp.",
        "",
        "## Target Overview",
        "",
        "| Component | Path | Language | Framework | Files | Functions | Classes | Exposed |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for name, data in model["components"].items():
        comp: Component = data["config"]
        lines.append("| {name} | `{path}` | {lang} | {fw} | {files} | {funcs} | {classes} | {exposed} |".format(
            name=name, path=comp.path, lang=comp.lang, fw=comp.framework or "-",
            files=len(data["files"]), funcs=len(data["functions"]), classes=len(data["classes"]),
            exposed=len(model["exposes"].get(name, [])),
        ))
    lines += ["", "### Node Counts", "", counts_table(node_counts), "", "### Edge Counts", "",
              counts_table(edge_counts), ""]

    for name, data in model["components"].items():
        comp: Component = data["config"]
        lines += [f"## Component: {name}", "", f"- Path: `{comp.path}`",
                  f"- Language/framework: {comp.lang}" + (f" / {comp.framework}" if comp.framework else ""),
                  ""]
        lines += ["### Module Groups", "",
                  "| Group | Files | Functions | Classes |",
                  "| --- | ---: | ---: | ---: |"]
        for group, counts in data["groups"].items():
            lines.append(f"| `{group}` | {counts['files']} | {counts['functions']} | {counts['classes']} |")
        if not data["groups"]:
            lines.append("| - | 0 | 0 | 0 |")
        lines += ["", "### Exposed Surface", ""]
        exposed = sorted(model["exposes"].get(name, []), key=lambda nid: (graph.nodes[nid]["kind"], node_label(graph, nid)))
        lines.extend(surface_table(graph, exposed, model))
        lines += ["", "### External Services Used", ""]
        lines.extend(bullet_list(sorted(model["services"].get(name, []))))
        lines += ["", "### Env Vars Read", ""]
        lines.extend(bullet_list(sorted(model["envs"].get(name, []))))
        normal, git = dependency_rows(graph, model["deps"].get(name, []))
        lines += ["", "### Direct Dependencies", ""]
        lines.extend(dep_table(normal))
        lines += ["", "Git dependencies:", ""]
        lines.extend(dep_table(git))
        lines.append("")

    lines += ["## Cross-Component Seams", "",
              "| Frontend symbol | Fetch | Proxy | Backend route | Handler |",
              "| --- | --- | --- | --- | --- |"]
    for seam in model["seams"]:
        fetch = " ".join(x for x in [seam["fetch_method"], seam["proxy_label"]] if x)
        lines.append("| {front} | `{fetch}` | `{proxy}` | `{route}` | `{handler}` |".format(
            front=md_escape(seam["frontend_label"]), fetch=md_escape(fetch),
            proxy=md_escape(seam["proxy_label"] if seam["proxy"] else "-"),
            route=md_escape(seam["route_label"]), handler=md_escape(seam["handler_label"] or "-"),
        ))
    if not model["seams"]:
        lines.append("| - | - | - | - | - |")

    lines += ["", "## Test Linkage Summary", "",
              "| Linkage level | TESTED_BY edges | Tested nodes |",
              "| --- | ---: | ---: |"]
    tested_nodes = defaultdict(set)
    for edge in graph.edges:
        if edge["kind"] == "TESTED_BY":
            tested_nodes[graph.nodes[edge["src"]]["kind"]].add(edge["src"])
    for kind in sorted(model["tested_by_kind"]):
        lines.append(f"| {kind} | {model['tested_by_kind'][kind]} | {len(tested_nodes[kind])} |")
    lines += ["", "Least-tested components by linked files/functions/classes:", "",
              "| Component | Linked nodes | Testable nodes | Linked % |",
              "| --- | ---: | ---: | ---: |"]
    for pct, covered, total, name in model["least_tested"]:
        lines.append(f"| {name} | {covered} | {total} | {pct:.1f}% |")

    lines += ["", "## Recency Summary", "",
              "| Component | Oldest touched file | Newest touched file |",
              "| --- | --- | --- |"]
    for name in sorted(model["recency"]):
        rec = model["recency"][name]
        lines.append(f"| {name} | {recency_cell(graph, rec['oldest'])} | {recency_cell(graph, rec['newest'])} |")
    lines += ["", "## Viz Data Notes", "",
              f"- `viz.html` embeds {len(model['viz']['nodes'])} nodes and {len(model['viz']['edges'])} edges.",
              f"- Aggregated {model['file_test_edges_aggregated']} file-level `TESTED_BY` edges into per-file counts; function/class-level `TESTED_BY` edges remain explicit.",
              ""]
    return "\n".join(lines)


def render_html(target: TargetConfig, graph: Graph, model: dict[str, Any]) -> str:
    data = {
        "target": target.name,
        "nodes": model["viz"]["nodes"],
        "edges": model["viz"]["edges"],
        "seams": model["viz"]["seams"],
        "notes": {
            "fileLevelTestedByAggregated": model["file_test_edges_aggregated"],
            "edgePolicy": "File-level TESTED_BY edges are aggregated into node props; other retained edges are explicit.",
        },
    }
    blob = json.dumps(data, sort_keys=True, separators=(",", ":")).replace("://", ":\\/\\/")
    return HTML_PREFIX + html.escape(blob, quote=False) + HTML_SUFFIX


HTML_PREFIX = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Architecture Map</title>
<style>
:root{color-scheme:light dark;--bg:#f7f8fb;--panel:#ffffff;--text:#1d2430;--muted:#657184;--line:#c9d2df;--accent:#2563eb;--sel:#f59e0b}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--panel:#191d24;--text:#e6eaf0;--muted:#9aa6b7;--line:#384252;--accent:#7aa2ff;--sel:#fbbf24}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:13px/1.35 system-ui,-apple-system,Segoe UI,sans-serif}
header{display:flex;gap:12px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line);background:var(--panel);position:sticky;top:0;z-index:2}
h1{font-size:16px;margin:0 8px 0 0}button,input{font:inherit}button{border:1px solid var(--line);background:var(--panel);color:var(--text);border-radius:6px;padding:6px 10px;cursor:pointer}button.active{border-color:var(--accent);color:var(--accent)}
input[type=search]{width:min(420px,32vw);border:1px solid var(--line);border-radius:6px;padding:7px 9px;background:var(--panel);color:var(--text)}
main{display:grid;grid-template-columns:1fr 340px;height:calc(100vh - 55px)}#stage{position:relative;overflow:hidden}svg{width:100%;height:100%;display:block}
aside{border-left:1px solid var(--line);background:var(--panel);overflow:auto;padding:12px}h2{font-size:14px;margin:0 0 8px}.muted{color:var(--muted)}.chips{display:flex;gap:8px;flex-wrap:wrap}.chip{display:inline-flex;gap:4px;align-items:center;white-space:nowrap}
.node rect{stroke:rgba(0,0,0,.18);stroke-width:1}.node text{fill:var(--text);font-size:11px;pointer-events:none}.node.highlight rect{stroke:var(--sel);stroke-width:3}.edge{stroke:var(--line);stroke-width:1.2;fill:none}.edge.highlight{stroke:var(--sel);stroke-width:2.5}
.legend{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;margin-top:8px}.sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px}
table{border-collapse:collapse;width:100%;font-size:12px}td,th{border-bottom:1px solid var(--line);padding:6px;text-align:left;vertical-align:top}tr.sel{background:color-mix(in srgb,var(--sel) 20%,transparent)}
pre{white-space:pre-wrap;word-break:break-word;background:var(--bg);padding:8px;border-radius:6px}.hidden{display:none}
</style>
</head>
<body>
<script id="graph-data" type="application/json">"""


HTML_SUFFIX = """</script>
<header>
<h1>Architecture Map</h1>
<button id="graphTab" class="active">Graph</button><button id="seamsTab">Seams</button>
<input id="search" type="search" placeholder="Search name, qname, path">
<div id="filters" class="chips"></div>
</header>
<main>
<section id="stage"><svg id="svg" role="img" aria-label="Layered architecture graph"></svg><div id="seams" class="hidden"></div></section>
<aside><h2>Details</h2><div id="details" class="muted">Click a node or seam.</div><h2>Legend</h2><div id="legend" class="legend"></div><p class="muted" id="foot"></p></aside>
</main>
<script>
"use strict";
const data=JSON.parse(document.getElementById("graph-data").textContent);
const colors={Component:"#7c8da6",ModuleGroup:"#8b9f3d",File:"#4f9d8f",Module:"#70a7d8",Class:"#bd8f52",Function:"#6f83d8",Route:"#d16a5b",Page:"#7aa36f",ApiProxy:"#d39a43",Dependency:"#9a89b8",Service:"#5aa6a6",EnvVar:"#b5748c",Test:"#8c8c8c"};
const nodeById=new Map(data.nodes.map(n=>[n.id,n]));
const kids=new Map(); for(const n of data.nodes){if(n.parent){if(!kids.has(n.parent))kids.set(n.parent,[]);kids.get(n.parent).push(n.id);}}
for(const arr of kids.values())arr.sort((a,b)=>(nodeById.get(a).label||a).localeCompare(nodeById.get(b).label||b));
let expanded=new Set(), selected="", highlighted=new Set(), mode="graph";
const kinds=[...new Set(data.nodes.map(n=>n.kind))].sort(); const enabled=new Set(kinds);
const svg=document.getElementById("svg"), details=document.getElementById("details");
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c]));}
function isVisible(id){const n=nodeById.get(id); if(!n||!enabled.has(n.kind))return false; if(!n.parent)return true; let p=n.parent; while(p){if(!expanded.has(p))return false; const pn=nodeById.get(p); p=pn&&pn.parent;} return true;}
function rep(id){let n=nodeById.get(id); while(n&&n.parent&&!isVisible(id)){id=n.parent; n=nodeById.get(id);} return id;}
function visibleNodes(){return data.nodes.filter(n=>isVisible(n.id));}
function layout(nodes){const byComp=new Map(); for(const n of nodes){const c=n.component||n.id; if(!byComp.has(c))byComp.set(c,[]); byComp.get(c).push(n);}
 const w=svg.clientWidth||900,h=svg.clientHeight||650,pos=new Map(); const comps=[...byComp.keys()].sort(); const colW=Math.max(220,w/Math.max(1,comps.length));
 comps.forEach((c,i)=>{const arr=byComp.get(c).sort((a,b)=>(a.parent||"").localeCompare(b.parent||"")||a.kind.localeCompare(b.kind)||a.label.localeCompare(b.label)); arr.forEach((n,j)=>{pos.set(n.id,{x:35+i*colW+(n.kind==="Component"?0:n.kind==="ModuleGroup"?18:n.kind==="File"?36:54),y:35+j*52,w:Math.min(190,colW-35),h:30});});});
 return pos;}
function render(){if(mode==="seams"){renderSeams();return;} document.getElementById("seams").classList.add("hidden"); svg.classList.remove("hidden"); svg.innerHTML="";
 const nodes=visibleNodes(), pos=layout(nodes), edgeAgg=new Map();
 for(const e of data.edges){const a=rep(e.src),b=rep(e.dst); if(a===b||!pos.has(a)||!pos.has(b))continue; const k=a+"→"+b+"|"+e.kind; edgeAgg.set(k,{src:a,dst:b,kind:e.kind,count:(edgeAgg.get(k)?.count||0)+1});}
 const gE=document.createElementNS("http"+"://www.w3.org/2000/svg","g"); svg.appendChild(gE);
 for(const e of [...edgeAgg.values()].sort((a,b)=>a.kind.localeCompare(b.kind)||a.src.localeCompare(b.src))){const p1=pos.get(e.src),p2=pos.get(e.dst); const path=document.createElementNS(svg.namespaceURI,"path"); path.setAttribute("class","edge"+(highlighted.has(e.src)||highlighted.has(e.dst)?" highlight":"")); path.setAttribute("d",`M${p1.x+p1.w} ${p1.y+p1.h/2} C${p1.x+p1.w+35} ${p1.y+p1.h/2}, ${p2.x-35} ${p2.y+p2.h/2}, ${p2.x} ${p2.y+p2.h/2}`); path.appendChild(document.createElementNS(svg.namespaceURI,"title")).textContent=`${e.kind} (${e.count})`; gE.appendChild(path);}
 const gN=document.createElementNS(svg.namespaceURI,"g"); svg.appendChild(gN);
 for(const n of nodes){const p=pos.get(n.id),g=document.createElementNS(svg.namespaceURI,"g"); g.setAttribute("class","node"+(highlighted.has(n.id)||selected===n.id?" highlight":"")); g.setAttribute("transform",`translate(${p.x},${p.y})`); g.onclick=()=>selectNode(n.id); const r=document.createElementNS(svg.namespaceURI,"rect"); r.setAttribute("width",p.w); r.setAttribute("height",p.h); r.setAttribute("rx","6"); r.setAttribute("fill",colors[n.kind]||"#999"); g.appendChild(r); const t=document.createElementNS(svg.namespaceURI,"text"); t.setAttribute("x","8"); t.setAttribute("y","19"); t.textContent=(kids.has(n.id)?(expanded.has(n.id)?"▾ ":"▸ "):"")+n.label.slice(0,28); g.appendChild(t); gN.appendChild(g);}}
function selectNode(id){selected=id; if(kids.has(id)){expanded.has(id)?expanded.delete(id):expanded.add(id);} highlighted=new Set([id]); const n=nodeById.get(id); const outs=data.edges.filter(e=>e.src===id).slice(0,20), ins=data.edges.filter(e=>e.dst===id).slice(0,20); details.innerHTML=`<b>${esc(n.label)}</b><p class="muted">${esc(n.kind)} ${esc(n.path||"")}</p><pre>${esc(JSON.stringify(n.props,null,2))}</pre><p><b>Out</b>: ${outs.length?outs.map(e=>esc(e.kind+" → "+(nodeById.get(e.dst)?.label||e.dst))).join("<br>"):"-"}</p><p><b>In</b>: ${ins.length?ins.map(e=>esc(e.kind+" ← "+(nodeById.get(e.src)?.label||e.src))).join("<br>"):"-"}</p>`; render();}
function renderSeams(){svg.classList.add("hidden"); const el=document.getElementById("seams"); el.classList.remove("hidden"); let rows=data.seams.map((s,i)=>`<tr data-i="${i}"><td>${esc(s.frontend_label)}</td><td>${esc(s.proxy_label)}</td><td>${esc(s.route_label)}</td><td>${esc(s.handler_label)}</td></tr>`).join(""); el.innerHTML=`<table><thead><tr><th>Frontend</th><th>Proxy</th><th>Route</th><th>Handler</th></tr></thead><tbody>${rows}</tbody></table>`; el.querySelectorAll("tr[data-i]").forEach(tr=>tr.onclick=()=>{const s=data.seams[+tr.dataset.i]; highlighted=new Set([s.frontend,s.proxy,s.route,s.handler].filter(Boolean)); selected=s.route; details.innerHTML=`<b>Seam</b><p>${esc(s.frontend_label)} → ${esc(s.route_label)} → ${esc(s.handler_label)}</p>`; el.querySelectorAll("tr").forEach(x=>x.classList.remove("sel")); tr.classList.add("sel");});}
document.getElementById("graphTab").onclick=()=>{mode="graph";graphTab.classList.add("active");seamsTab.classList.remove("active");render();};
document.getElementById("seamsTab").onclick=()=>{mode="seams";seamsTab.classList.add("active");graphTab.classList.remove("active");render();};
document.getElementById("search").oninput=e=>{const q=e.target.value.toLowerCase(); if(!q){highlighted=new Set();render();return;} const hits=data.nodes.filter(n=>[n.label,n.name,n.qname,n.path,n.id].join(" ").toLowerCase().includes(q)); highlighted=new Set(hits.map(n=>n.id)); if(hits[0]){let p=hits[0].parent; while(p){expanded.add(p); p=nodeById.get(p)?.parent;} selected=hits[0].id; details.innerHTML=`${hits.length} match(es). First: <b>${esc(hits[0].label)}</b>`;} render();};
const filters=document.getElementById("filters"); for(const k of kinds){const label=document.createElement("label"); label.className="chip"; label.innerHTML=`<input type="checkbox" checked> ${esc(k)}`; label.querySelector("input").onchange=e=>{e.target.checked?enabled.add(k):enabled.delete(k);render();}; filters.appendChild(label);}
document.getElementById("legend").innerHTML=kinds.map(k=>`<span><i class="sw" style="background:${colors[k]||"#999"}"></i>${esc(k)}</span>`).join("");
document.getElementById("foot").textContent=`Embedded ${data.nodes.length} nodes and ${data.edges.length} edges. Aggregated ${data.notes.fileLevelTestedByAggregated} file-level TESTED_BY edges.`;
render(); addEventListener("resize",render);
</script>
</body></html>
"""


def component_for_node(target: TargetConfig, nid: str, node: dict[str, Any]) -> str:
    if nid.startswith("component:"):
        return nid.removeprefix("component:")
    props = node["props"]
    if props.get("component"):
        return str(props["component"])
    path = str(props.get("path", ""))
    for comp in sorted(target.components, key=lambda c: len(c.path), reverse=True):
        if path == comp.path or path.startswith(comp.path.rstrip("/") + "/"):
            return comp.name
    if node["kind"] == "Dependency":
        return ""
    return ""


def module_group(comp: Component, path: str) -> str:
    rel = path.removeprefix(comp.path).lstrip("/")
    if not rel:
        return "."
    parts = rel.split("/")
    return parts[0] if len(parts) == 1 else parts[0]


def module_group_name(target: TargetConfig, comp_name: str, path: str) -> str:
    comp = next((c for c in target.components if c.name == comp_name), None)
    return module_group(comp, path) if comp else ""


def node_label(graph: Graph, nid: str) -> str:
    if not nid:
        return ""
    node = graph.nodes.get(nid)
    if not node:
        return nid
    props = node["props"]
    if node["kind"] == "Route":
        return route_label(graph, nid)
    if node["kind"] in {"Page", "ApiProxy"}:
        return str(props.get("route") or props.get("path") or nid)
    if node["kind"] in {"Dependency", "Service", "EnvVar"}:
        return str(props.get("name") or nid.split(":", 1)[-1])
    return str(props.get("qname") or props.get("name") or props.get("path") or nid)


def symbol_label(graph: Graph, nid: str) -> str:
    if not nid:
        return ""
    node = graph.nodes.get(nid)
    if not node:
        return nid
    path = node["props"].get("path", "")
    name = node["props"].get("qname") or node["props"].get("name") or nid.split("::")[-1]
    return f"{Path(path).name}::{name}" if path else str(name)


def route_label(graph: Graph, nid: str) -> str:
    node = graph.nodes.get(nid, {})
    props = node.get("props", {})
    method = props.get("method")
    path = props.get("path")
    if method and path:
        return f"{method} {path}"
    return nid.removeprefix("route:")


def surface_table(graph: Graph, exposed: list[str], model: dict[str, Any]) -> list[str]:
    if not exposed:
        return ["No exposed routes, pages, or proxies in the indexed facts."]
    lines = ["| Kind | Surface | Handler / Target | Guards |",
             "| --- | --- | --- | --- |"]
    for nid in exposed:
        kind = graph.nodes[nid]["kind"]
        handlers = ", ".join(symbol_label(graph, h) for h in sorted(model["route_handlers"].get(nid, []))) or "-"
        guards = ", ".join(symbol_label(graph, g) for g in sorted(model["route_guards"].get(nid, []))) or "-"
        if kind in {"Page", "ApiProxy"}:
            handlers = graph.nodes[nid]["props"].get("backend_path", "-")
        lines.append(f"| {kind} | `{md_escape(node_label(graph, nid))}` | `{md_escape(handlers)}` | `{md_escape(guards)}` |")
    return lines


def dependency_rows(graph: Graph, deps: list[tuple[str, dict[str, Any]]]) -> tuple[list[tuple], list[tuple]]:
    normal, git = [], []
    for dep_id, props in sorted(deps, key=lambda d: node_label(graph, d[0])):
        row = (node_label(graph, dep_id), props.get("declared_version", ""),
               props.get("locked_version", ""), props.get("source", ""))
        (git if str(row[3]).startswith("git") or dep_id.startswith("dep:git/") else normal).append(row)
    return normal, git


def dep_table(rows: list[tuple]) -> list[str]:
    lines = ["| Dependency | Declared | Locked | Source |",
             "| --- | --- | --- | --- |"]
    if not rows:
        lines.append("| - | - | - | - |")
    for name, declared, locked, source in rows:
        lines.append(f"| `{md_escape(name)}` | `{md_escape(declared or '-')}` | `{md_escape(locked or '-')}` | `{md_escape(source or '-')}` |")
    return lines


def bullet_list(items: list[str]) -> list[str]:
    return [f"- `{md_escape(i)}`" for i in items] if items else ["- None in indexed facts."]


def counts_table(counts: Counter) -> str:
    lines = ["| Kind | Count |", "| --- | ---: |"]
    for kind in sorted(counts):
        lines.append(f"| {kind} | {counts[kind]} |")
    return "\n".join(lines)


def recency_cell(graph: Graph, items: list[tuple[str, str]]) -> str:
    if not items:
        return "-"
    date, fid = items[0]
    path = graph.nodes[fid]["props"].get("path", fid)
    return f"`{md_escape(date)}` `{md_escape(path)}`"


def md_escape(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
