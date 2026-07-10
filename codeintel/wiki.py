"""Graph-grounded narrative wiki generator for codeintel targets."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from codeintel import OUT_DIR, TARGETS_DIR
from codeintel.config import Component, ConfigError, TargetConfig, load_target_by_name
from codeintel.mapgen import Graph, build_model, load_facts, md_escape, node_label, symbol_label
from codeintel.monitor import latest_snapshot_info


DEFAULT_MODEL = "google/gemini-3.5-flash"
STATE_FILE = ".state.json"
RUN_FILE = ".last-run.json"
CONTEXT_LIMIT = 8192
FOOTER = "Generated from graph snapshot {ts} - facts, not vibes; verify semantics in source."
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class WikiError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PageSpec:
    name: str
    filename: str
    title: str
    kind: str
    component: str = ""


@dataclass
class LLMResult:
    text: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None


def cli_run(args: argparse.Namespace) -> int:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        target = load_target_by_name(args.target, TARGETS_DIR)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        api_key = _openrouter_key()
    except WikiError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code

    model = args.model or os.environ.get("CODEINTEL_WIKI_MODEL") or DEFAULT_MODEL
    try:
        report = generate_wiki(target, model, api_key, force=args.force, page=args.page)
    except WikiError as e:
        print(f"error: {e}", file=sys.stderr)
        return e.code

    for line in report["lines"]:
        print(line)
    print(
        "summary: {written} written, {skipped} up to date, {failed} failed, "
        "{calls} LLM calls, {tokens} tokens, {seconds:.1f}s".format(
            written=report["written"],
            skipped=report["skipped"],
            failed=len(report["failed"]),
            calls=report["llm_calls"],
            tokens=report["total_tokens"] if report["total_tokens"] is not None else "unknown",
            seconds=report["seconds"],
        )
    )
    return 1 if report["failed"] else 0


def generate_wiki(
    target: TargetConfig,
    model: str,
    api_key: str,
    *,
    force: bool = False,
    page: str | None = None,
) -> dict[str, Any]:
    facts_dir = OUT_DIR / target.name / "facts"
    if not facts_dir.is_dir():
        raise WikiError(f"no facts at {facts_dir}; run `codeintel index {target.name}` first")
    graph = load_facts(facts_dir)
    model_data = build_model(target, graph)
    snapshot = latest_snapshot_info(target.name)
    snapshot_ts = snapshot["timestamp"] if snapshot else "unknown"
    findings = _load_findings(target.name)
    pages = _page_specs(target)
    if page:
        wanted = _page_filename(page)
        pages = [p for p in pages if p.filename == wanted or p.name == page]
        if not pages:
            raise WikiError(f"unknown wiki page {page!r}")

    out_dir = OUT_DIR / target.name / "wiki"
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / STATE_FILE
    state = _read_state(state_path)

    started = time.time()
    lines: list[str] = []
    failed: list[str] = []
    written = 0
    skipped = 0
    llm_calls = 0
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens: int | None = 0

    with httpx.Client(timeout=90.0) as client:
        for spec in pages:
            pack = _context_pack(target, graph, model_data, findings, snapshot_ts, spec)
            digest = _hash(pack)
            previous = state.get(spec.filename, {})
            if (
                not force
                and previous.get("input_hash") == digest
                and previous.get("model") == model
                and previous.get("snapshot_ts") == snapshot_ts
                and (out_dir / spec.filename).exists()
            ):
                skipped += 1
                lines.append(f"{spec.filename}: up to date")
                continue
            prompt = _prompt(spec, pack)
            llm_calls += 1
            try:
                result = _call_llm(client, api_key, model, prompt)
            except WikiError as e:
                failed.append(f"{spec.filename}: {e}")
                lines.append(f"{spec.filename}: failed ({e})")
                continue
            if result.prompt_tokens is not None:
                prompt_tokens += result.prompt_tokens
            if result.completion_tokens is not None:
                completion_tokens += result.completion_tokens
            if result.total_tokens is None:
                total_tokens = None
            elif total_tokens is not None:
                total_tokens += result.total_tokens
            body = _assemble_page(spec, result.text, snapshot_ts, pack)
            (out_dir / spec.filename).write_text(body, encoding="utf-8")
            state[spec.filename] = {
                "input_hash": digest,
                "model": model,
                "snapshot_ts": snapshot_ts,
            }
            _write_state(state_path, state)
            written += 1
            lines.append(f"{spec.filename}: wrote")

    run = {
        "target": target.name,
        "model": model,
        "llm_calls": llm_calls,
        "prompt_tokens": prompt_tokens if llm_calls else 0,
        "completion_tokens": completion_tokens if llm_calls else 0,
        "total_tokens": total_tokens,
        "seconds": round(time.time() - started, 3),
        "failed": failed,
    }
    (out_dir / RUN_FILE).write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "lines": lines,
        "written": written,
        "skipped": skipped,
        "failed": failed,
        "llm_calls": llm_calls,
        "total_tokens": total_tokens,
        "seconds": run["seconds"],
    }


def _page_specs(target: TargetConfig) -> list[PageSpec]:
    pages = [
        PageSpec("index", "index.md", f"{target.name} Wiki", "index"),
        PageSpec("architecture", "architecture.md", "Architecture", "architecture"),
    ]
    for comp in sorted(target.components, key=lambda c: c.name):
        pages.append(PageSpec(f"component:{comp.name}", f"{_slug(comp.name)}.md", f"Component: {comp.name}", "component", comp.name))
    pages.extend([
        PageSpec("seams", "seams.md", "Cross-Component Seams", "seams"),
        PageSpec("getting-started", "getting-started.md", "Getting Started", "getting-started"),
    ])
    return pages


def _context_pack(
    target: TargetConfig,
    graph: Graph,
    model: dict[str, Any],
    findings: list[dict[str, Any]],
    snapshot_ts: str,
    spec: PageSpec,
) -> str:
    base = {
        "target": target.name,
        "snapshot_ts": snapshot_ts,
        "page": spec.filename,
        "source_policy": "This pack is assembled from codeintel facts, graph edges, snapshots, and findings only. It contains no target source code.",
    }
    if spec.kind == "index":
        data = {**base, **_overview_pack(target, graph, model), "reading_guide_pages": [p.filename for p in _page_specs(target)]}
    elif spec.kind == "architecture":
        data = {
            **base,
            **_overview_pack(target, graph, model),
            "component_edges": _component_edges(target, graph, model),
            "mermaid": _mermaid_component_seams(target, graph, model),
        }
    elif spec.kind == "component":
        data = {**base, **_component_pack(target, graph, model, findings, spec.component)}
    elif spec.kind == "seams":
        data = {**base, "seams": _seam_rows(graph, model, limit=40), "component_edges": _component_edges(target, graph, model)}
    elif spec.kind == "getting-started":
        data = {**base, "infra": _infra_pack(target, graph, model)}
    else:
        data = base
    text = json.dumps(data, indent=2, sort_keys=True)
    if len(text.encode("utf-8")) <= CONTEXT_LIMIT:
        return text
    data["_truncated"] = "Context was compacted to stay under 8KB."
    return _compact_json(data, CONTEXT_LIMIT)


def _overview_pack(target: TargetConfig, graph: Graph, model: dict[str, Any]) -> dict[str, Any]:
    node_counts = Counter(n["kind"] for n in graph.nodes.values())
    edge_counts = Counter(e["kind"] for e in graph.edges)
    comps = []
    for name, data in model["components"].items():
        comp: Component = data["config"]
        comps.append({
            "name": name,
            "path": comp.path,
            "language": comp.lang,
            "framework": comp.framework or "",
            "files": len(data["files"]),
            "functions": len(data["functions"]),
            "classes": len(data["classes"]),
            "exposed_count": len(model["exposes"].get(name, [])),
            "dependencies": [node_label(graph, dep) for dep, _props in model["deps"].get(name, [])[:12]],
            "services": sorted(model["services"].get(name, []))[:12],
            "env_vars": sorted(model["envs"].get(name, []))[:12],
        })
    return {
        "components": comps,
        "node_counts": dict(sorted(node_counts.items())),
        "edge_counts": dict(sorted(edge_counts.items())),
        "seam_count": len(model["seams"]),
    }


def _component_pack(
    target: TargetConfig,
    graph: Graph,
    model: dict[str, Any],
    findings: list[dict[str, Any]],
    name: str,
) -> dict[str, Any]:
    data = model["components"][name]
    comp: Component = data["config"]
    exposed = sorted(model["exposes"].get(name, []), key=lambda nid: (graph.nodes[nid]["kind"], node_label(graph, nid)))
    deps_normal = []
    deps_git = []
    for dep_id, props in sorted(model["deps"].get(name, []), key=lambda row: node_label(graph, row[0])):
        row = {
            "name": node_label(graph, dep_id),
            "declared": props.get("declared_version", ""),
            "locked": props.get("locked_version", ""),
            "source": props.get("source", ""),
        }
        (deps_git if str(row["source"]).startswith("git") or dep_id.startswith("dep:git/") else deps_normal).append(row)
    return {
        "component": {
            "name": name,
            "path": comp.path,
            "language": comp.lang,
            "framework": comp.framework or "",
            "files": len(data["files"]),
            "functions": len(data["functions"]),
            "classes": len(data["classes"]),
            "module_groups": data["groups"],
        },
        "exposed_surface": [_surface_row(graph, model, nid) for nid in exposed[:30]],
        "key_symbols_by_inbound_calls": _key_symbols(graph, model, name, limit=20),
        "seams_touching_component": _component_seams(graph, model, name, limit=20),
        "services": sorted(model["services"].get(name, []))[:30],
        "env_vars": sorted(model["envs"].get(name, []))[:40],
        "dependencies": deps_normal[:30],
        "git_dependencies": deps_git[:20],
        "findings_summary": _findings_for_component(findings, comp.path, limit=10),
        "recency": _recency_for_component(graph, model, name),
    }


def _surface_row(graph: Graph, model: dict[str, Any], nid: str) -> dict[str, Any]:
    return {
        "kind": graph.nodes[nid]["kind"],
        "surface": node_label(graph, nid),
        "path": graph.nodes[nid]["props"].get("path", ""),
        "handler_or_target": ", ".join(symbol_label(graph, h) for h in sorted(model["route_handlers"].get(nid, [])))
        or graph.nodes[nid]["props"].get("backend_path", ""),
        "guards": [symbol_label(graph, g) for g in sorted(model["route_guards"].get(nid, []))],
    }


def _key_symbols(graph: Graph, model: dict[str, Any], comp: str, limit: int) -> list[dict[str, Any]]:
    inbound_counts = Counter(e["dst"] for e in graph.edges if e["kind"] == "CALLS")
    rows = [(inbound_counts[nid], nid) for nid in model["components"][comp]["functions"] + model["components"][comp]["classes"]]
    rows.sort(key=lambda r: (-r[0], graph.nodes[r[1]]["props"].get("path", ""), graph.nodes[r[1]]["props"].get("line") or 0, r[1]))
    result = []
    for inbound, nid in rows[:limit]:
        node = graph.nodes[nid]
        result.append({
            "symbol": symbol_label(graph, nid),
            "kind": node["kind"],
            "path": node["props"].get("path", ""),
            "line": node["props"].get("line", ""),
            "inbound_calls": inbound,
        })
    return result


def _component_seams(graph: Graph, model: dict[str, Any], comp: str, limit: int) -> list[dict[str, Any]]:
    rows = []
    for seam in model["seams"]:
        route_comp = _node_component(model, seam["route"])
        front_comp = _node_component(model, seam["frontend"])
        handler_comp = _node_component(model, seam["handler"])
        if comp in {route_comp, front_comp, handler_comp}:
            rows.append(_seam_row(graph, seam))
    return rows[:limit]


def _seam_rows(graph: Graph, model: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    return [_seam_row(graph, seam) for seam in model["seams"][:limit]]


def _seam_row(graph: Graph, seam: dict[str, Any]) -> dict[str, Any]:
    return {
        "frontend": seam["frontend_label"],
        "frontend_path": graph.nodes.get(seam["frontend"], {}).get("props", {}).get("path", ""),
        "fetch_method": seam["fetch_method"],
        "proxy": seam["proxy_label"] if seam["proxy"] else "",
        "route": seam["route_label"],
        "handler": seam["handler_label"],
        "handler_path": graph.nodes.get(seam["handler"], {}).get("props", {}).get("path", ""),
    }


def _infra_pack(target: TargetConfig, graph: Graph, model: dict[str, Any]) -> dict[str, Any]:
    env_by_file: dict[str, list[str]] = defaultdict(list)
    service_by_file: dict[str, list[str]] = defaultdict(list)
    for edge in graph.edges:
        if edge["kind"] == "READS_ENV":
            env_by_file[graph.nodes[edge["src"]]["props"].get("path", edge["src"])].append(node_label(graph, edge["dst"]))
        elif edge["kind"] == "USES_SERVICE":
            service_by_file[graph.nodes[edge["src"]]["props"].get("path", edge["src"])].append(node_label(graph, edge["dst"]))
    infra_files = []
    for nid, node in graph.nodes.items():
        if node["kind"] != "File":
            continue
        path = str(node["props"].get("path", ""))
        lower = path.lower()
        if any(marker in lower for marker in ("dockerfile", "docker-compose", "compose.", "pyproject.toml", "package.json", "uvicorn", "manage.py", "main.py", "run.py", "cli.py")):
            infra_files.append(path)
    return {
        "components": [
            {"name": c.name, "path": c.path, "language": c.lang, "framework": c.framework or ""}
            for c in sorted(target.components, key=lambda c: c.name)
        ],
        "env_vars_by_component": {name: sorted(values)[:40] for name, values in sorted(model["envs"].items())},
        "services_by_component": {name: sorted(values)[:30] for name, values in sorted(model["services"].items())},
        "env_vars_by_file": {k: sorted(v) for k, v in sorted(env_by_file.items())[:40]},
        "services_by_file": {k: sorted(v) for k, v in sorted(service_by_file.items())[:40]},
        "infra_or_entry_files_from_graph": sorted(infra_files)[:40],
    }


def _component_edges(target: TargetConfig, graph: Graph, model: dict[str, Any]) -> list[dict[str, Any]]:
    counts: Counter[tuple[str, str, str]] = Counter()
    for edge in graph.edges:
        src = _node_component(model, edge["src"])
        dst = _node_component(model, edge["dst"])
        if src and dst and src != dst and edge["kind"] not in {"CONTAINS", "DEFINES"}:
            counts[(src, dst, edge["kind"])] += 1
    return [
        {"src": src, "dst": dst, "kind": kind, "count": count}
        for (src, dst, kind), count in sorted(counts.items())
    ]


def _mermaid_component_seams(target: TargetConfig, graph: Graph, model: dict[str, Any]) -> str:
    lines = ["```mermaid", "flowchart LR"]
    for comp in sorted(target.components, key=lambda c: c.name):
        lines.append(f"  {_mermaid_id(comp.name)}[{_mermaid_label(comp.name)}]")
    edge_counts: Counter[tuple[str, str, str]] = Counter()
    for row in _component_edges(target, graph, model):
        if row["kind"] in {"FETCHES", "PROXIES", "HANDLED_BY", "DEPENDS_ON", "USES_SERVICE", "READS_ENV", "IMPORTS"}:
            edge_counts[(row["src"], row["dst"], row["kind"])] += int(row["count"])
    for seam in model["seams"]:
        src = _node_component(model, seam["frontend"])
        dst = _node_component(model, seam["handler"])
        if src and dst and src != dst:
            edge_counts[(src, dst, "SEAM")] += 1
    for (src, dst, kind), count in sorted(edge_counts.items()):
        label = f"{kind} x{count}"
        lines.append(f"  {_mermaid_id(src)} -->|{_mermaid_label(label)}| {_mermaid_id(dst)}")
    if len(lines) == 2:
        lines.append("  empty[No cross-component edges captured]")
    lines.append("```")
    return "\n".join(lines)


def _node_component(model: dict[str, Any], node_id: str) -> str:
    return model["comp_for_id"].get(node_id, "")


def _recency_for_component(graph: Graph, model: dict[str, Any], name: str) -> dict[str, Any]:
    rec = model["recency"].get(name, {})
    return {
        "oldest": [_dated_file(graph, item) for item in rec.get("oldest", [])],
        "newest": [_dated_file(graph, item) for item in rec.get("newest", [])],
    }


def _dated_file(graph: Graph, item: tuple[str, str]) -> dict[str, str]:
    date, fid = item
    return {"date": date, "path": graph.nodes[fid]["props"].get("path", fid)}


def _findings_for_component(findings: list[dict[str, Any]], comp_path: str, limit: int) -> list[dict[str, Any]]:
    prefix = comp_path.rstrip("/") + "/"
    rows = []
    for item in findings:
        evidence = item.get("evidence", [])
        if any(str(ev).startswith(prefix) or str(ev).startswith(comp_path + ":") for ev in evidence):
            rows.append({
                "id": item.get("id", ""),
                "category": item.get("category", ""),
                "severity": item.get("severity", ""),
                "confidence": item.get("confidence", ""),
                "title": item.get("title", ""),
                "evidence": evidence[:3],
            })
    return rows[:limit]


def _prompt(spec: PageSpec, context_pack: str) -> str:
    return f"""Write the markdown body for `{spec.filename}` in a graph-grounded engineering wiki.

Hard rules:
- Use ONLY facts in the JSON context pack below.
- Cite file paths exactly as they appear in the pack when making implementation claims.
- If a semantic detail is missing, write "not captured in the graph" rather than inventing.
- No marketing tone, no hype, no source-code quotations, and no claims about target source code not present in the pack.
- Keep it concise and useful for a maintainer.
- Do not include the generated footer; the tool appends it.
- Do not wrap the whole answer in a code block.

Page focus: {spec.title}

JSON context pack:
{context_pack}
"""


def _assemble_page(spec: PageSpec, prose: str, snapshot_ts: str, pack: str) -> str:
    body = prose.strip()
    if spec.kind == "architecture":
        data = json.loads(pack)
        mermaid = data.get("mermaid", "")
        if mermaid and mermaid not in body:
            body = body.rstrip() + "\n\n## Deterministic Component/Seams Diagram\n\n" + mermaid
    if not body.startswith("#"):
        body = f"# {spec.title}\n\n{body}"
    footer = FOOTER.format(ts=snapshot_ts)
    return body.rstrip() + "\n\n---\n" + footer + "\n"


def _call_llm(client: httpx.Client, api_key: str, model: str, prompt: str) -> LLMResult:
    payload = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": "You write factual software architecture prose from supplied graph facts only."},
            {"role": "user", "content": prompt},
        ],
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://codeintel.local",
        "X-Title": "codeintel wiki",
    }
    last_error = ""
    for attempt in range(2):
        try:
            resp = client.post(OPENROUTER_URL, headers=headers, json=payload)
        except httpx.HTTPError as e:
            last_error = str(e)
            if attempt == 0:
                time.sleep(1.0)
                continue
            raise WikiError(f"OpenRouter request failed: {last_error}") from None
        if resp.status_code in {429} or 500 <= resp.status_code <= 599:
            last_error = f"HTTP {resp.status_code}"
            if attempt == 0:
                retry_after = _retry_after(resp)
                time.sleep(retry_after)
                continue
        if resp.status_code >= 400:
            raise WikiError(f"OpenRouter returned HTTP {resp.status_code}")
        doc = resp.json()
        text = doc.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = doc.get("usage") or {}
        if not text.strip():
            raise WikiError("OpenRouter returned an empty response")
        return LLMResult(
            text=text,
            prompt_tokens=_int_or_none(usage.get("prompt_tokens")),
            completion_tokens=_int_or_none(usage.get("completion_tokens")),
            total_tokens=_int_or_none(usage.get("total_tokens")),
        )
    raise WikiError(f"OpenRouter returned {last_error}")


def _openrouter_key() -> str:
    def _lookup() -> str | None:
        # OPENROUTER_API_KEY is the provider's canonical name; the underscored
        # variant matches ~/.env on this host.
        return os.environ.get("OPENROUTER_API_KEY") or os.environ.get("OPEN_ROUTER_API_KEY")

    key = _lookup()
    if not key:
        env_path = Path("/home/hermes/.env")
        if env_path.exists():
            try:
                from dotenv import load_dotenv

                load_dotenv(env_path)
            except Exception:
                _load_env_file(env_path)
        key = _lookup()
    if not key:
        raise WikiError("OPENROUTER_API_KEY (or OPEN_ROUTER_API_KEY) is not set; "
                        "add it to the environment or /home/hermes/.env")
    return key


def _load_env_file(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        if name and name not in os.environ:
            os.environ[name] = value.strip().strip("\"'")


def _load_findings(target: str) -> list[dict[str, Any]]:
    path = OUT_DIR / target / "findings" / "findings.json"
    if not path.exists():
        return []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return doc if isinstance(doc, list) else []


def _read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return doc if isinstance(doc, dict) else {}


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compact_json(data: dict[str, Any], limit: int) -> str:
    shrink = json.loads(json.dumps(data))
    preferred_limits = {
        "seams": 15,
        "exposed_surface": 15,
        "key_symbols_by_inbound_calls": 12,
        "dependencies": 15,
        "env_vars": 20,
        "env_vars_by_file": 20,
        "infra_or_entry_files_from_graph": 20,
        "findings_summary": 6,
    }
    for key, max_items in preferred_limits.items():
        _limit_nested(shrink, key, max_items)
    text = json.dumps(shrink, indent=2, sort_keys=True)
    if len(text.encode("utf-8")) <= limit:
        return text
    shrink["_truncated_hard"] = "Pack was further reduced by dropping long lists."
    for key in list(shrink):
        if isinstance(shrink[key], (list, dict)) and key not in {"target", "snapshot_ts", "page", "source_policy", "component", "components", "node_counts", "edge_counts"}:
            shrink[key] = [] if isinstance(shrink[key], list) else {}
            text = json.dumps(shrink, indent=2, sort_keys=True)
            if len(text.encode("utf-8")) <= limit:
                return text
    return text[:limit]


def _limit_nested(value: Any, key: str, max_items: int) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k == key:
                if isinstance(v, list):
                    value[k] = v[:max_items]
                elif isinstance(v, dict):
                    value[k] = dict(list(v.items())[:max_items])
            else:
                _limit_nested(v, key, max_items)
    elif isinstance(value, list):
        for item in value:
            _limit_nested(item, key, max_items)


def _retry_after(resp: httpx.Response) -> float:
    value = resp.headers.get("retry-after")
    if value:
        try:
            return min(10.0, max(1.0, float(value)))
        except ValueError:
            pass
    return 2.0


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-").lower()
    return slug or "page"


def _page_filename(value: str) -> str:
    return value if value.endswith(".md") else f"{_slug(value)}.md"


def _mermaid_id(value: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_]+", "_", value)
    if not clean or clean[0].isdigit():
        clean = "n_" + clean
    return clean


def _mermaid_label(value: str) -> str:
    return md_escape(value).replace('"', "'")
