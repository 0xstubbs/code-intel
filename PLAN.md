# Code-Intelligence Platform — v1.0 Implementation Plan

*Status: awaiting approval. Research basis: `research/landscape.md`, `research/beancounter-inventory.md`.*

## 1. Recommendation: **Hybrid** — adopt a graph-indexer core, build a thin platform around it

**Adopt:** [CodeGraphContext](https://github.com/CodeGraphContext/CodeGraphContext) (MIT, active, v0.5.x) as the structural extraction core: tree-sitter parsing of Python/TS/TSX → graph DB (Kuzu or FalkorDB Lite embedded; Neo4j optional) with calls/imports/inheritance/defs, `watch` incremental re-index, CLI + MCP server, built-in HTML graph viz.

**Build (the platform proper, all project-agnostic):**
- **Target-as-config** harness — a target is a YAML file, never code.
- **Domain extractors** the core lacks: FastAPI routes, Next.js pages/API-proxy routes, **frontend-fetch ↔ backend-route seams**, dependency/lockfile layer (uv + npm, git deps), infra/config surface (Dockerfiles, env schema, external services).
- **Unified query surface** over one store (canned queries + raw graph queries + JSON artifacts + MCP).
- **Map + visualization generator** (architecture map doc + self-contained interactive HTML).
- **Monitoring/regeneration harness** with drift reports.

**Why not pure adopt:** every 2026 candidate stops at the symbol/call graph — none models infra, lockfiles, cross-language seams, multi-component targets, or drift (see landscape.md Tier 1 gaps). **Why not pure build:** tree-sitter→graph-DB→MCP→viz already exists, maintained, MIT; rebuilding it is waste.

**Core selection — one open decision, settled empirically in W1.** The hermes deep crawl surfaced a second strong core: **codebase-memory-mcp** (DeusData; 29.1k★, MIT, single binary, 158 langs, semantic cross-file resolution for Py/TS, read-only openCypher + 14 MCP tools, built-in change detection). CGC remains the *default* pick because its backends (Neo4j/Kuzu/FalkorDB) are **writable** — our domain extractors merge route/seam/dep/infra facts into the same graph — whereas codebase-memory-mcp's store is internal and read-only. W1 runs both against Beancounter behind the adapter (a day's bake-off): if codebase-memory-mcp's extraction quality is decisively better, we keep it as the read-optimized structural source and hold our facts in a sidecar graph; otherwise CGC. Either way the adapter interface is the contract.

**Risk containment:** the core sits behind a `GraphStore`/`StructuralCore` adapter; our extractors write into our documented schema; if the chosen core stalls, the fallback is the other finalist, code-review-graph, or direct tree-sitter extraction — a swap, not a rebuild. Rejected alternatives and why: Sourcegraph (closed-source 2024), CodeQL (license bars private code), GitNexus (PolyForm Noncommercial), Glean (Haskell stack, self-host burden), Joern (security-shaped CPG; possible later *analysis plugin*), Kythe (maintenance mode).

## 2. Where it lives & parameterization

**Standalone project at `/home/hermes/code-intel/`** (own git repo, own uv venv on Python ≥3.12 — independent of Beancounter's 3.11). *Not* inside the Beancounter repo: the platform is multi-target by design, and keeping it out preserves the read-only stance toward targets. Beancounter is `targets/beancounter.yaml`, nothing more.

```yaml
# targets/beancounter.yaml (illustrative)
name: beancounter
root: /home/hermes/beancounter
exclude: [node_modules, .venv, __pycache__, _archive, dogfood-output, extracted, logs]
components:
  - {name: backend,  path: ui/backend,  lang: python,     framework: fastapi}
  - {name: frontend, path: ui/frontend, lang: typescript, framework: nextjs}
  - {name: tools,    path: ampro_tools, lang: python}
  - {name: e2e,      path: ui/e2e,      lang: javascript}
extractors: [structure, python-api, nextjs, seams, dependencies, infra, tests, git-meta]
```

New target = new YAML. New analysis = new extractor module registered in a plugin registry (entry-point style), reading the same config and writing to the same store.

## 3. Architecture

```
targets/*.yaml ─▶ codeintel CLI ─▶ EXTRACTION ─▶ REPRESENTATION ─▶ OUTPUTS
                                   ├ structure (CGC adapter:        graph DB (Kuzu/Falkor):    ├ map: ARCHITECTURE-MAP.md
                                   │   defs/calls/imports/inherit)  File/Module/Function/       ├ viz: interactive HTML
                                   ├ python-api (FastAPI routes)    Class/Route/Page/            │   (self-contained)
                                   ├ nextjs (pages, proxies)        Dependency/Service/          ├ query: codeintel query
                                   ├ seams (fetch ↔ route join)     Env nodes + CALLS/           │   (canned + raw Cypher)
                                   ├ dependencies (uv/npm locks)    IMPORTS/EXPOSES/             ├ MCP server (agents)
                                   └ infra (Docker/env/services)    FETCHES/DEPENDS_ON/          └ JSON artifacts
                                                                    CONFIGURED_BY edges     ─▶ MONITORING
                                                                    + JSON sidecar artifacts     codeintel refresh / drift
```

- **Extraction** — CGC provides the structural base graph per component; our extractors are small tree-sitter/AST passes producing typed *facts* (JSON) merged into the graph. Extractor registry makes new analyses additive.
- **Representation** — one embedded graph DB per target under `out/<target>/`, plus the JSON fact files (greppable/agent-readable without a DB client). Graph schema is ours and documented — the durable asset; adopted tools are implementation details behind it.
- **Visualization** — layered map: components → modules → symbols, plus a seams view (page → fetch → route → handler → DB/service). Self-contained HTML; CGC's per-query viz available underneath.
- **Query** — `codeintel query <name|raw>` canned questions ("what handles POST /api/upload", "what depends on X") + raw graph queries + CGC's MCP server so agents query instead of grepping.

## 4. Monitoring & drift

- `codeintel index <target>` — full (re)build; `codeintel refresh <target>` — incremental re-extract + **drift report** (nodes/edges/routes/deps added-removed-changed vs. last snapshot, stored under `out/<target>/snapshots/`).
- CGC `watch` mode available for live sessions.
- CI/pre-commit wiring: **documented as a later step**, not built in v1 (a `refresh --check` exit code makes it trivial to add).

## 5. Phase-2 subagent decomposition

Orchestrator (me) integrates; workers are scoped, context-isolated:

| Worker | Scope | Output |
|---|---|---|
| W1 platform-core | Skeleton, `pyproject`, config loader, extractor registry, CGC adapter; index Beancounter structure | working `codeintel index beancounter` |
| W2 app-extractors | python-api (FastAPI routes + auth-guard/dependency tagging), nextjs, seams extractors | Route/Page/Seam facts in graph |
| W3 supply-extractors | dependencies (uv.lock/package-lock, git deps), infra (Dockerfiles, env schema, external-service touchpoints), tests (static test→symbol linkage), git-meta (last-modified per file/symbol) | Dependency/Infra/Test/Recency facts |
| W4 map-and-viz | ARCHITECTURE-MAP.md generator + interactive HTML viz | initial Beancounter map |
| W5 monitor-and-docs | refresh/drift/snapshots, second-target smoke test, usage + extension guides | verified regeneration + docs |

Sequencing: W1 → (W2 ∥ W3) → (W4 ∥ W5). Orchestrator gates each merge by running the pipeline end-to-end and sample queries.

## 6. Definition of done (v1.0 — an MVP baseline meant to grow, not a finished product)

1. `codeintel index beancounter && codeintel map beancounter` runs end-to-end from the YAML config.
2. **Project-agnostic proven, not asserted:** the same commands run against a second target (e.g. `eat_tools/`) with only a new YAML.
3. Initial Beancounter map + interactive visualization **generated by the platform**.
4. Representation demonstrably rich & queryable: a documented set of ~10 sample queries answers structural, dependency, and cross-language-seam questions (incl. "page → fetch → FastAPI handler → DB", "what depends on X", "symbols with no inbound edges", "code with no test linkage", "routes and their auth guards"), via CLI and MCP — proving the later findings passes (dead/stale/untested/permissions) are cheap queries, not new infrastructure.
5. Regeneration verified: touch a target file → `refresh` → drift report shows exactly that change.
6. Docs: usage guide, "point it at a new codebase" guide, "add a new analysis" guide, schema reference.
7. Beancounter source untouched (read-only guarantee holds).

**Explicitly out of scope:** running analyses / producing findings (dead code, security, staleness…) — that's the next pass *on top of* this instrument.
