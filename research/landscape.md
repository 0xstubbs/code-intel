# Phase 0b — 2026 Code-Intelligence Landscape

*Research wiki, 2026-07-09. Direct web research by Claude; a complementary hermes-agent/Firecrawl deep-crawl writes to `hermes-landscape.md` when it completes. Question: does a robust platform already exist that parses a codebase into a queryable graph, visualizes it, and keeps it fresh — for a Python 3.11 FastAPI + TypeScript/Next.js monorepo?*

## TL;DR
The category exploded in 2025–2026 around **MCP code-graph servers** (tree-sitter → graph DB → agent-queryable). Several are real, maintained, MIT-licensed, and cover Python+TS. None covers the whole platform goal (infra/config extraction, dependency/SBOM layer, cross-language seams, target-as-config multi-project harness, drift reports). **Recommendation: hybrid — adopt a graph-indexer core, build a thin orchestration/extension layer around it.**

---

## Tier 1 — Adoptable cores (local-first code-graph engines)

### CodeGraphContext (CGC) — ⭐ primary candidate
- **Repo:** github.com/CodeGraphContext/CodeGraphContext · **MIT** · v0.5.1 · ~3.9k★, 1.5k commits, 17 releases, ~31k PyPI dl/mo (Jun 2026) — active org with many contributors.
- **Extracts:** functions, classes, methods, params, inheritance, calls, imports via **tree-sitter** (23 langs incl. Python, TypeScript, TSX) + optional **SCIP** for precision.
- **Storage:** pluggable graph DBs — FalkorDB Lite (default, Unix, needs Py ≥3.12), KuzuDB/LadybugDB, **Neo4j**, Nornic. Real graph DB ⇒ open-ended **Cypher-family queries**, not just canned tools.
- **Interfaces:** CLI (`codegraphcontext index .`, `analyze calls X --viz`) + **MCP server**; `watch .` for live incremental re-index; standalone-HTML interactive graph viz.
- **Gaps:** graph schema thinly documented; no infra/config/deps modeling; young (0.x).

### code-review-graph
- tirth8205/code-review-graph · **MIT** · ~17–19k★. Tree-sitter, 30+ languages incl. Py/TS/TSX. **SQLite** store, ~34 MCP tools, auto-updates on edit, blast-radius analysis, optional embeddings.
- Strong, but tuned to *review token-efficiency*; fixed tool surface over SQLite rather than an open graph query language. Good fallback core.

### code-graph-mcp (sdsrss)
- **Uniquely relevant feature:** `trace_http_chain` — maps `GET /api/x` → handler → service → DB across **FastAPI**/Express/Go; plus `impact_analysis`, `dependency_graph`, semantic search. SQLite+FTS5+sqlite-vec, BLAKE3 Merkle incremental indexing. Very active (256 releases, v0.92.0 Jul 2026) but **~50★, single maintainer** — bus-factor risk. Treat as design reference for our seam extractor, not as the core.

### GitNexus — rejected
- 28k+★, MCP-native, LadybugDB, hybrid search. **PolyForm Noncommercial license** (commercial use requires paid tier) + browser/client-side architecture. License alone disqualifies it as a foundation.

## Tier 2 — Heavyweight platforms (capable, poor fit for v1)

| Tool | Verdict |
|---|---|
| **Glean** (Meta, facebookincubator/Glean) | Typed facts + Angle datalog; indexes Python/TS *via Sourcegraph's SCIP indexers*. Industrial-strength but a **Haskell service stack**; self-hosting burden far exceeds v1 needs. Revisit at multi-million-LOC scale. |
| **Joern** (joernio/joern) | Code property graphs, Scala query DSL, actively maintained (commits Jun 2026), supports Python/JS/TS. Security-analysis-shaped; CPG queries have a steep DSL; general structure mapping is not its center of gravity. Candidate *analysis plugin* later (security pass), not the core. |
| **CodeQL** (GitHub) | **License prohibits** use on non-OSS/private code without GitHub Advanced Security / commercial license. Out. |
| **Kythe** (Google) | US team laid off Apr 2024; maintenance mode. Out. |
| **Sourcegraph** | Closed-source since 2023–24 (repo private Aug 2024); Cody Free/Pro terminated Jul 2025, enterprise-only ($59/user/mo). Out — **but its SCIP indexers (`scip-python`, `scip-typescript`) remain Apache-2.0** and are the de-facto precise-symbol standard (Glean and CGC both consume them). Usable building block. |
| **CodeScene / Understand (SciTools)** | Commercial, per-seat, closed; behavioral/metrics focus. Not adoptable as an extensible core. |
| **Sourcetrail** | Dead upstream (archived 2021); forks exist but niche/desktop-only. Out. |

## Tier 3 — Building blocks (for the layer we build)

- **tree-sitter** — the 2026 substrate for all of Tier 1; use directly for custom extractors (FastAPI decorators, Next.js routes, fetch seams).
- **SCIP indexers** — `scip-python`, `scip-typescript` (Apache-2.0): precise cross-file symbol resolution if/when tree-sitter heuristics fall short.
- **dependency-cruiser** (JS/TS, active, MIT) — import graph + *rule enforcement* + DOT/JSON output; understands TS path aliases. Frontend module-graph + future architectural-rules layer.
- **pydeps / grimp / tach** — Python import graphs; grimp gives a programmatic API; tach adds boundary enforcement.
- **syft** (Anchore, Apache-2.0) — SBOM from lockfiles (handles npm + can be complemented for uv) ; **osv-scanner** (Google) — known-vuln/staleness audit over lockfiles. Dependency layer.
- **emerge / CodeCharta / DependaCharta** (MaibornWolff) — multi-language dependency viz & code-city maps; maintained; candidates for visualization inspiration or direct reuse of their JSON formats.
- **Codebase-Memory (arXiv 2603.27277)** — validates the architecture: tree-sitter multi-phase pipeline → knowledge graph gives order-of-magnitude token-efficiency gains for agent code exploration vs. grepping.

## Build vs. adopt vs. hybrid — assessment

**Pure adopt** fails: every Tier-1 tool stops at the symbol/call graph. None models infra/config, lockfile/SBOM state, cross-language HTTP seams as first-class data, multi-component targets-as-config, or drift reporting. **Pure build** is waste: tree-sitter indexing into a graph DB with watch mode, MCP, and viz already exists, maintained, MIT.

**⇒ Hybrid.** Adopt **CodeGraphContext** as the structural-graph core (MIT license, active org, real graph DB with open query language, watch mode, viz, MCP — and Python, same language as our glue). Build a thin, project-agnostic orchestration layer around it: target-as-config, domain extractors (routes/seams/infra/deps), unified query surface, map generation, drift/regeneration harness. Keep the core swappable behind an interface in case CGC (0.x) stalls — fallback: code-review-graph, or direct tree-sitter extraction into the same schema.

### Key risks
1. **CGC is 0.x** — mitigate: pin version; isolate behind an adapter; our own extractors write to the same graph, so the core is replaceable.
2. **FalkorDB Lite needs Python ≥3.12** — platform gets its own uv-managed venv (3.12+), independent of Beancounter's 3.11; or use Kuzu/Neo4j backend.
3. **Category churn** (fast-moving 2026 space) — the thin-layer design means we own the schema and config; adopted components are implementation details.

## Reconciliation with hermes-agent deep crawl (`hermes-landscape.md`)

The hermes/Firecrawl crawl (completed 2026-07-09 20:40) largely corroborates the above, with three deltas:

1. **`codebase-memory-mcp` (DeusData) is a serious Tier-1 candidate I under-weighted.** Verified directly on GitHub: **29.1k★, MIT, v0.9.0 (2026-07-08)**, single static binary, 158 tree-sitter languages with hybrid-LSP semantic cross-file resolution for 9 (incl. Python, TS/TSX), SQLite store, 14 MCP tools **plus a read-only openCypher subset**, built-in change detection, sub-ms queries. (My earlier secondary source, ChatForest, listed 2.4k★ — wrong/outdated.) Trade-off vs CGC for our purposes: its store is internal and its query language **read-only**, so our domain extractors (routes/seams/deps/infra) can't merge facts into the same graph; CGC's Neo4j/Kuzu/FalkorDB backends are writable. Decision deferred to a W1 bake-off (see PLAN §1).
2. **Hermes error — CodeQL license.** Hermes reports "MIT, fully self-hostable, no restrictions." The github/codeql repo (queries + libraries) is MIT, but the **CLI binaries are under the proprietary GitHub CodeQL Terms**, which bar analysis of non-OSS/private code without GHAS or a commercial license (verified: codeql-cli-binaries/LICENSE.md, github/codeql#4025). CodeQL remains out.
3. **Hermes error — Glean is Haskell, not Hack** (facebookincubator/Glean is majority Haskell). Verdict unchanged: operationally heavy, out for v1.

Hermes additionally surfaced `roam-code`, `codeseek`, and `code-graph-rag` (Memgraph) — noted in its wiki; none displace the finalists.

## Sources
- https://github.com/CodeGraphContext/CodeGraphContext · https://codegraphcontext.github.io/ · https://pypi.org/project/codegraphcontext/
- https://github.com/tirth8205/code-review-graph · https://chatforest.com/reviews/code-intelligence-codebase-graph-mcp-servers/
- https://github.com/sdsrss/code-graph-mcp
- https://github.com/abhigyanpatwari/GitNexus · https://www.marktechpost.com/2026/04/24/meet-gitnexus-an-open-source-mcp-native-knowledge-graph-engine-that-gives-claude-code-and-cursor-full-codebase-structural-awareness/
- https://github.com/facebookincubator/Glean · https://engineering.fb.com/2024/12/19/developer-tools/glean-open-source-code-indexing/ · https://glean.software/docs/indexer/scip-python/
- https://github.com/joernio/joern · https://docs.joern.io/
- https://github.com/github/codeql-cli-binaries/blob/main/LICENSE.md · https://github.com/github/codeql/issues/4025
- https://en.wikipedia.org/wiki/Google_Kythe · https://github.com/kythe/kythe
- https://en.wikipedia.org/wiki/Sourcegraph · https://sourcegraph.com/blog/announcing-scip
- https://github.com/glato/emerge · https://github.com/MaibornWolff/DependaCharta
- https://arxiv.org/html/2603.27277v1 (Codebase-Memory)
