# Code Intelligence Platform Landscape — July 2026

> Research compiled 2026-07-09 by live GitHub API + web inspection.
> Focus: tools that parse codebases into queryable graphs (symbols, deps, call graphs),
> support Python 3.11+ and TypeScript/React, and are relevant for agent+human consumption.

---

## Executive Summary

The code intelligence landscape has bifurcated sharply in 2025–2026:

1. **Big-tech indexers** (Sourcegraph/SCIP, Glean, Kythe, CodeQL, Joern) remain the
   gold standard for correctness but carry heavy self-hosting burdens.
2. **MCP-native code-graph servers** have exploded — `codebase-memory-mcp` (29k stars)
   and `code-review-graph` (19k stars) are the new heavyweights, built specifically
   for AI coding agents. These are tree-sitter-based, light-weight, and pre-packaged
   for Claude Code / Codex CLI.
3. **Visualization tools** (Sourcetrail, Emerge) have largely stagnated. The energy
   has shifted to MCP servers that expose graph data programmatically rather than
   rendering it.

For the Beancounter use case (Python 3.11 + TypeScript/React, agent-driven querying),
the best combination is likely an MCP-native code graph server paired with a SCIP
indexer for maximum accuracy.

---

## Tier 1: Big-Tech Indexers (Highest Accuracy, Heaviest Burden)

### Sourcegraph + SCIP

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Symbols, references, call hierarchy, type hierarchy, cross-repo search |
| **Languages** | All SCIP-indexed languages: Python (scip-python), TypeScript/JS (scip-typescript), Go, Rust, Java, C++, C#, Kotlin, Ruby, Zig, Odin, + more |
| **License** | **SCIP protocol + indexers: Apache-2.0**. Sourcegraph server: proprietary (formerly Apache-2.0; repo made private ~2025) |
| **Maintenance** | ACTIVE. scip repo moved to `scip-code/scip` — v0.8.2 released last week (July 2026). scip-python and scip-typescript both active (commits within days). |
| **Extensibility** | SCIP is a protocol with protobuf schema. Write a new indexer in any language. SCIP→LSIF bridge exists. |
| **Self-hosting** | Sourcegraph server is enterprise-only (SaaS or on-prem license). SCIP indexers run standalone: `scip-python index`, `scip-typescript index`. Output is a single `index.scip` file. |
| **Key URLs** | https://github.com/scip-code/scip · https://github.com/sourcegraph/scip-python · https://github.com/sourcegraph/scip-typescript · https://sourcegraph.com |
| **Verdict** | SCIP indexers are the most **accurate** per-language indexers available and are fully open-source. The Sourcegraph MCP server (`sg_keyword_search`) is commercial. For self-hosted agent use, run SCIP indexers + consume the SCIP file in your own MCP server. |

### Glean (Meta)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Symbol info (location, type, relationships), cross-references, call hierarchies, type hierarchies. Uses the Angle query language. |
| **Languages** | **Direct indexers**: C++, Hack, Haskell, Python, JavaScript/Flow. **SCIP/LSIF bridge**: Rust, Go, TypeScript, Java, .NET (Python via scip-python also). |
| **License** | BSD (3-clause) |
| **Maintenance** | ACTIVE. 1,361 stars at `facebookincubator/Glean`. Last push July 9, 2026. |
| **Extensibility** | Pluggable indexers. SCIP/LSIF ingestion means any SCIP-capable language works. Write new indexers in any language against the Glean schema. |
| **Self-hosting** | Heavy. Written in Hack (Meta's PHP dialect) + C++. Requires a Thrift service, RocksDB, and significant operational knowledge. Docker image available for demo. |
| **Key URLs** | https://github.com/facebookincubator/Glean · https://glean.software |
| **Verdict** | Technically excellent but operationally heavy. The Hack dependency is a barrier — few people outside Meta run Hack. Best consumed via the SCIP bridge (feed it SCIP indexes from external indexers). |

### Kythe (Google)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Full cross-reference graph: definitions, references, callers/callees, overrides, documentation. Graph-oriented schema. |
| **Languages** | C++, Java (production extractors). Go (extractor). Rust (active development, commit yesterday). No Python or TypeScript indexer in-tree. |
| **License** | Apache-2.0 |
| **Maintenance** | ACTIVE but niche. `kythe/kythe` (moved from `google/kythe`), 2,121 stars, 5,552 commits. Committed yesterday (July 8, 2026). |
| **Extensibility** | Pluggable indexer framework — write an extractor+compiler for any language. Strong schema. |
| **Self-hosting** | Very heavy. Bazel-based build. Requires a serving pipeline (xrefs server, file tree, identifier search). Google-scale infrastructure assumptions. |
| **Key URLs** | https://github.com/kythe/kythe · https://kythe.io |
| **Verdict** | No Python or TypeScript indexers exist today. Adding them is non-trivial. Unless you're already a Bazel/Google-infra shop, better to look elsewhere. |

### CodeQL (GitHub)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | AST-level representation + data-flow + control-flow. Queryable via QL (declarative logic language). Primarily security-focused but can query any code property. |
| **Languages** | Python, JavaScript/TypeScript, C/C++, C#, Go, Java/Kotlin, Ruby, Swift, Rust |
| **License** | **MIT** (core engine + standard libraries). GitHub Advanced Security integration is commercial. |
| **Maintenance** | ACTIVE. 9,806 stars, pushed July 9, 2026. 1,414 open issues. |
| **Extensibility** | Write custom QL queries. Extend the standard libraries. The extractor (converting source → database) is per-language and can be custom-built. |
| **Self-hosting** | Medium. `codeql database create --language=python` produces a queryable DB. `codeql database analyze` runs queries. No server needed. The DB is a directory of compressed tables — not a live graph service. |
| **Key URLs** | https://github.com/github/codeql · https://codeql.github.com/docs/codeql-cli/ |
| **Verdict** | MIT licensing makes this **fully self-hostable** with no restrictions. It's a CLI tool, not a live graph server — you build databases, run queries, get results. Excellent for security analysis; less suited for continuous agent-driven graph queries. Python + TypeScript support is mature. |

### Joern (Code Property Graphs)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Code Property Graphs: AST + CFG + PDG + DDG merged into a single graph. Queryable via a custom Scala DSL or Cypher-like queries. |
| **Languages** | C/C++, Java/JVM, JavaScript, Python, Kotlin, LLVM bitcode, x86 binary (via Ghidra), Ruby, PHP, Swift |
| **License** | Apache-2.0 |
| **Maintenance** | VERY ACTIVE. 3,314 stars, pushed July 9, 2026 (today). 343 open issues. Scala ecosystem. Discord community. |
| **Extensibility** | Write custom passes ("enhancements") that add new node/edge types. CPG schema is extensible. |
| **Self-hosting** | Medium. JVM-based (Scala). `joern-cli` is a single fat JAR. Runs locally. Export to Neo4j for visualization. `joern-parse` + `joern` gives an interactive shell. |
| **Key URLs** | https://github.com/joernio/joern · https://joern.io · https://cpg.joern.io |
| **Verdict** | Excellent for security/vulnerability research. The CPG is the most comprehensive single-graph representation (AST+CFG+PDG+DDG). Python and JS support exist. The Scala/JVM requirement is a consideration. |

---

## Tier 2: Tree-sitter-Based Structural Tools

### ast-grep

| Attribute | Value |
|-----------|-------|
| **What it extracts** | AST-level pattern matching (structural search). Find code by shape, not regex. Supports lint rules and rewrites. Not a graph — a search tool. |
| **Languages** | ~20 languages via tree-sitter: Python, TS/JS, Rust, Go, Java, C/C++, C#, Ruby, Kotlin, Swift, Lua, + more |
| **License** | MIT |
| **Maintenance** | VERY ACTIVE. 15,000 stars, pushed today. 41 open issues. |
| **Extensibility** | Write custom rules in YAML (sgconfig). Use as a library (Rust crate) or CLI. |
| **Self-hosting** | Trivial. Single binary. `sg scan` / `sg run`. |
| **Key URLs** | https://github.com/ast-grep/ast-grep |
| **Verdict** | Excellent for structural search and codemods. Not a code graph — no dependency tracking, call graphs, or cross-file analysis. Complements a graph tool but doesn't replace one. |

### dependency-cruiser

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Module-level dependency graph for JS/TS. Visualizes imports/exports. Detects circular dependencies. Validates architecture rules (e.g., "ui may not import from db"). |
| **Languages** | JavaScript, TypeScript, JSX, TSX, Vue, CoffeeScript |
| **License** | MIT |
| **Maintenance** | ACTIVE. 6,911 stars, pushed today. 37 open issues. |
| **Extensibility** | Rule-based — define allowed/disallowed dependency patterns in config. Graphviz/D3/JSON output. |
| **Self-hosting** | Trivial. `npm install dependency-cruiser`. `npx depcruise src`. |
| **Key URLs** | https://github.com/sverweij/dependency-cruiser |
| **Verdict** | Best-in-class for JS/TS module dependency analysis. Python not supported. Excellent for enforcing architecture boundaries in a TS monorepo. |

### madge

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Module dependency graph for JS/TS. Simpler than dependency-cruiser — just the graph. |
| **Languages** | JavaScript, TypeScript (CommonJS, AMD, ES6) |
| **License** | MIT |
| **Maintenance** | STALE. 10,126 stars but last pushed January 2026 (6 months). 124 open issues. |
| **Verdict** | Use dependency-cruiser instead for JS/TS. |

### pydeps

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Python module import dependency graph. SVG/PNG visualization. |
| **Languages** | Python only |
| **License** | BSD-2-Clause |
| **Maintenance** | ACTIVE. 2,105 stars, pushed July 6, 2026. 44 open issues. |
| **Extensibility** | CLI flags for filtering (exclude stdlib, show only certain modules). Graphviz output. |
| **Self-hosting** | Trivial. `pip install pydeps`. `pydeps mypackage`. |
| **Key URLs** | https://github.com/thebjorn/pydeps |
| **Verdict** | Good for quick Python dependency visualization. Not a queryable graph — just a one-shot rendering. No cross-language support. |

---

## Tier 3: MCP-Native Code Intelligence (The 2025–2026 Wave)

These are new tools built specifically for AI coding agents, exposing code graph
data through MCP (Model Context Protocol) tools. They're the most relevant
category for agent-driven code understanding.

### codebase-memory-mcp (DeusData)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Full knowledge graph: symbols, references, dependencies, call graphs. Persistent SQLite-backed index. Sub-millisecond queries. |
| **Languages** | **158 languages** via tree-sitter. Single static binary. |
| **License** | MIT |
| **Maintenance** | EXTREMELY ACTIVE. **29,105 stars** (the largest code intel MCP tool), pushed today. |
| **Extensibility** | MCP tools for `codebase_search`, `codebase_symbol_info`, `codebase_dependencies`. Used by Claude Code, Codex CLI, Gemini CLI, Cursor, Windsurf, Aider, KiloCode, OpenCode. |
| **Self-hosting** | Trivial. Single static binary, no dependencies. `npx @deusdata/codebase-memory-mcp` or download binary. |
| **Key URLs** | https://github.com/DeusData/codebase-memory-mcp |
| **Verdict** | **Top recommendation for agent-driven code intelligence.** 158 languages, zero config, MIT license, huge community. The trade-off: tree-sitter-based extraction is less precise than SCIP/Glean for complex cross-file resolution (generics, overloads, dynamic dispatch). But for 95% of agent use cases, this is more than sufficient. |

### code-review-graph (tirth8205)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Persistent codebase map for AI tools: symbols, references, call graphs. Incremental updates. GraphRAG-style retrieval. |
| **Languages** | Python, TypeScript, JavaScript via tree-sitter |
| **License** | MIT |
| **Maintenance** | ACTIVE. **19,356 stars**, pushed June 2026. |
| **Extensibility** | MCP + CLI. Benchmark-backed context reduction for large repos. |
| **Self-hosting** | Light. Python package. |
| **Key URLs** | https://github.com/tirth8205/code-review-graph |
| **Verdict** | Second-largest MCP code graph tool. Benchmark-validated context reduction claims. Python-native, good for Python-first projects. |

### code-graph-rag (vitali87)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Multi-language knowledge graph into Memgraph graph DB. Tree-sitter parsing. Hybrid RAG (graph + semantic search). |
| **Languages** | Python, JavaScript, TypeScript, Rust, Go, Java, C/C++, + more (claims "multi-language monorepo" support) |
| **License** | MIT |
| **Maintenance** | ACTIVE. 2,305 stars, pushed today. 29 open issues. |
| **Extensibility** | MCP server. Graph DB backend (Memgraph for Cypher queries). Vector embeddings for semantic search. |
| **Self-hosting** | Medium. Requires Memgraph (graph DB) or can be configured for other backends. Python package. |
| **Key URLs** | https://github.com/vitali87/code-graph-rag |
| **Verdict** | The most feature-rich MCP code graph: graph DB + vector search + MCP. Higher setup cost (requires Memgraph) but more query power. |

### codanna (bartolli)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Local code intelligence: symbols, references, call graphs. |
| **Languages** | Not explicitly stated — tree-sitter-based, likely many. |
| **License** | Apache-2.0 |
| **Maintenance** | ACTIVE. 707 stars, pushed July 4, 2026. Written in Rust. |
| **Key URLs** | https://github.com/bartolli/codanna |
| **Verdict** | Solid Rust-based MCP server. Smaller community but active. |

### codeseek (CodeBendKit)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Call graphs + hybrid semantic search (Dense + Sparse + RRF + Reranker). Built specifically for AI coding agents. |
| **Languages** | 7 languages via tree-sitter (Python, TS/JS, Rust, Go, Java, C/C++, C#) |
| **License** | MIT |
| **Maintenance** | ACTIVE. 614 stars, pushed July 8, 2026. Rust binary. |
| **Extensibility** | Native MCP tools for Claude Code + Codex CLI. |
| **Self-hosting** | Trivial. Single Rust binary. |
| **Key URLs** | https://github.com/CodeBendKit/codeseek |
| **Verdict** | Good balance: call graphs + semantic search + MCP. Smaller language set (7) but covers Python + TS. |

### roam-code (Cranot)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | SQLite code graph: 238 commands, 224 MCP tools. Symbols, dependencies, change-safety gates, audit evidence. |
| **Languages** | 28 languages via tree-sitter |
| **License** | Apache-2.0 |
| **Maintenance** | ACTIVE. 495 stars, pushed today. Python package. |
| **Extensibility** | MCP + CLI. Zero API keys needed. Change-safety gates prevent breaking changes. |
| **Self-hosting** | Trivial. Python package, SQLite backend. |
| **Key URLs** | https://github.com/Cranot/roam-code |
| **Verdict** | Most comprehensive MCP tool count (224). Python-native. Good for teams wanting both CLI and MCP access. |

### code-graph-mcp (sdsrss)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | AST knowledge graph: semantic search, call graph traversal, HTTP route tracing, impact analysis. |
| **Languages** | 10 languages via tree-sitter |
| **License** | Not specified (no license file) |
| **Maintenance** | ACTIVE. 50 stars, pushed July 6, 2026. Rust binary. |
| **Key URLs** | https://github.com/sdsrss/code-graph-mcp |
| **Verdict** | Smaller project but has HTTP route tracing — useful for web apps. No license is a concern for integration. |

---

## Tier 4: Visualization-Focused Tools

### Sourcetrail (CoatiSoftware)

| Attribute | Value |
|-----------|-------|
| **Status** | **ARCHIVED since December 2021.** 16,483 stars, C++, GPL-3.0. No active forks with meaningful traction. |
| **Successors** | No major successor. `codescope` (MalyisGreat, 0 stars, July 2026) claims to be a "local, zero-config successor" for TS/JS. `sloptrail` (1 star) is another attempt. `quarkslab/numbat` (24 stars) allows reading Sourcetrail DBs. |
| **Verdict** | Dead. The UI concept (interactive graph explorer) was excellent, but no maintained fork exists. |
| **Key URLs** | https://github.com/CoatiSoftware/Sourcetrail |

### Emerge (glato/emerge)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Interactive browser-based codebase and dependency visualization. Basic code quality and graph metrics. |
| **Languages** | C, Go, Java, JavaScript, Kotlin, Python, Ruby, Swift, TypeScript |
| **License** | Not specified |
| **Maintenance** | **STALE.** 1,118 stars, last pushed October 2024 (20 months). |
| **Extensibility** | Visualization only — no query API or MCP. |
| **Key URLs** | https://github.com/glato/emerge |
| **Verdict** | Stale but functional. Useful for one-off visualization, not for agent-driven querying. |

### CodeCharta (MaibornWolff)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | 3D city-map visualization of code metrics (complexity, LOC, coupling). Imports from SonarQube, CodeScene, generic CSV. |
| **Languages** | Language-agnostic (consumes metrics, not source) |
| **License** | BSD-3-Clause |
| **Maintenance** | ACTIVE. 474 stars, pushed today. 110 open issues. |
| **Extensibility** | Importer API for custom metric sources. |
| **Self-hosting** | Light. TypeScript/Angular app. |
| **Key URLs** | https://github.com/MaibornWolff/codecharta |
| **Verdict** | Visualization-only. Needs external analysis tools to feed it metrics. Not a code graph itself. |

### fallow (fallow-rs)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Deep static analysis: unused code, duplication, circular dependencies, complexity hotspots, architecture boundaries, design-system drift. Optional paid runtime layer. |
| **Languages** | TypeScript, JavaScript only |
| **License** | MIT |
| **Maintenance** | ACTIVE. **4,059 stars**, pushed today. Rust binary. |
| **Self-hosting** | Trivial. Single Rust binary. Free tier covers static analysis. |
| **Key URLs** | https://github.com/fallow-rs/fallow |
| **Verdict** | Excellent for TS/JS code health. The free static analysis is thorough. No Python support — TS/JS only. |

---

## Tier 5: Commercial Tools

### CodeScene

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Behavioral code analysis: technical debt, knowledge distribution, delivery risk, code health trends. Uses git history + static analysis. |
| **Languages** | Language-agnostic (works on git history) + specific language parsers |
| **License** | Commercial SaaS. Free tier for open-source projects. Student/developer packages available. |
| **Maintenance** | ACTIVE. Well-funded company. |
| **Self-hosting** | SaaS only (no on-prem in free tier). |
| **Key URLs** | https://codescene.com |
| **Verdict** | Not a code graph tool. It analyzes git history for organizational/process insights. Useful for management, not for agent-driven code understanding. |

### Understand (SciTools)

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Static analysis IDE: dependency graphs, metrics, code navigation, refactoring, MISRA/AUTOSAR compliance. |
| **Languages** | C/C++, C#, Java, Python, JavaScript, TypeScript, Ada, Fortran, + many more |
| **License** | **Commercial.** Per-seat licensing. |
| **Maintenance** | ACTIVE. Established product (decades). |
| **Self-hosting** | Desktop application. No server/API offering for agent consumption. |
| **Key URLs** | https://scitools.com |
| **Verdict** | Powerful but commercial and desktop-only. No MCP/API for agent consumption. Overkill for most web-app codebases. |

### Sourcegraph (Enterprise)

| Attribute | Value |
|-----------|-------|
| **Status** | Fully commercial now. SaaS platform + on-prem enterprise licenses. Their MCP server (`sg_keyword_search`) connects coding agents to the Sourcegraph index. Stripe case study prominently featured. |
| **Key URLs** | https://sourcegraph.com |
| **Verdict** | Best-in-class enterprise code intelligence. The SCIP indexers remain open-source and can be used independently. The commercial server is expensive but the gold standard if budget allows. |

---

## Tier 6: Adjacent / Niche

### sem + weave (Ataraxy-Labs)

| Attribute | Value |
|-----------|-------|
| **What they do** | `sem`: Entity-level semantic diffs on top of git (28 languages via tree-sitter). `weave`: Entity-level git merge driver (95% reduction in false merge conflicts). |
| **License** | Apache-2.0 |
| **Maintenance** | ACTIVE. sem: 3,084 stars, pushed July 8, 2026. weave: 1,217 stars, pushed July 9, 2026. |
| **Verdict** | Not code graphs per se, but entity-level awareness. `sem` in particular could feed an entity-level code graph. Excellent for agent collaboration scenarios. |
| **Key URLs** | https://github.com/Ataraxy-Labs/sem · https://github.com/Ataraxy-Labs/weave |

### FalkorDB code-graph

| Attribute | Value |
|-----------|-------|
| **What it extracts** | Demo of code graph RAG using FalkorDB (graph DB) and GraphRAG-SDK. |
| **Languages** | Python (demo) |
| **License** | Not specified |
| **Maintenance** | ACTIVE. 324 stars, pushed today. |
| **Verdict** | It's a demo/reference implementation, not a production tool. Shows the FalkorDB+GraphRAG pattern. |
| **Key URLs** | https://github.com/FalkorDB/code-graph |

### Neo4j-based code graph projects

Several small projects exist (eviking/codeKG, hypen-code/code-spider, barryAlan2633/codegraph) that index code into Neo4j and expose it via MCP. All are 0–1 stars as of July 2026. No dominant Neo4j code-graph project has emerged.

### Grafeas

**Not a code intelligence tool.** Grafeas (grafeas/grafeas, 1,567 stars) is an artifact metadata API — it stores metadata about container images, build provenance, and vulnerability scans. Wrong category for this research.

---

## Language Support Matrix: Python 3.11+ and TypeScript/React

| Tool | Python | TypeScript/React | Agent-Friendly | Self-Host Burden |
|------|--------|------------------|----------------|-----------------|
| **SCIP indexers** | YES (scip-python) | YES (scip-typescript) | MCP via Sourcegraph (commercial) or custom | Light (indexers only) |
| **Glean** | YES (direct + SCIP) | YES (via SCIP bridge) | Angle query language | Heavy (Hack+Thrift) |
| **Kythe** | NO indexer | NO indexer | Not applicable | Very heavy (Bazel) |
| **CodeQL** | YES | YES | CLI only, no live graph | Medium |
| **Joern** | YES (basic) | YES (basic) | Cypher-like DSL | Medium (JVM) |
| **codebase-memory-mcp** | YES | YES | YES (MCP, 158 langs) | Trivial (binary) |
| **code-review-graph** | YES | YES | YES (MCP) | Light (Python) |
| **code-graph-rag** | YES | YES | YES (MCP) | Medium (Memgraph) |
| **codeseek** | YES | YES | YES (MCP) | Trivial (binary) |
| **roam-code** | YES | YES | YES (MCP, 224 tools) | Light (Python+SQLite) |
| **dependency-cruiser** | NO | YES (excellent) | No | Trivial (npm) |
| **pydeps** | YES | NO | No | Trivial (pip) |
| **ast-grep** | YES | YES | No | Trivial (binary) |
| **fallow** | NO | YES | No | Trivial (binary) |

---

## Recommendations for Beancounter (Python 3.11 + TypeScript/React)

### Short-term (immediate agent value)

**codebase-memory-mcp** (29k stars, MIT, 158 languages) is the clear winner for
immediate agent integration. Single binary, zero config, both Python and
TypeScript covered. The tree-sitter precision is adequate for ~95% of agent
queries. Run it alongside your coding agent: `npx @deusdata/codebase-memory-mcp`.

### Medium-term (higher accuracy)

Add **scip-python** and **scip-typescript** as a secondary indexer for when you
need high-precision cross-file resolution (generics, overloads, dynamic dispatch).
Write a thin MCP wrapper that reads the SCIP protobuf and exposes `scip_definition`,
`scip_references`, `scip_callers`. SCIP indexers are Apache-2.0 and can run in CI
on every commit.

### Visualization (human exploration)

**CodeCharta** (BSD-3-Clause) for city-map visualization if you want a dashboard.
Feed it metrics from the tools above. For dependency visualization specifically,
**dependency-cruiser** (TS) + **pydeps** (Python) produce Graphviz output you can
render.

### What NOT to use

- **Sourcetrail**: Archived since 2021. No maintained successor.
- **Kythe**: No Python or TypeScript indexers. Heavy Bazel dependency.
- **Grafeas**: Not a code intelligence tool.
- **Sourcegraph server**: Commercial/expensive. The SCIP indexers are free.

---

## Sources

All data verified via GitHub API and web inspection on 2026-07-09:

- https://github.com/scip-code/scip — SCIP protocol (Apache-2.0, active)
- https://github.com/sourcegraph/scip-python — Python SCIP indexer (active)
- https://github.com/sourcegraph/scip-typescript — TypeScript SCIP indexer (active)
- https://github.com/facebookincubator/Glean — Meta Glean (BSD, active)
- https://github.com/kythe/kythe — Google Kythe (Apache-2.0, active)
- https://github.com/github/codeql — GitHub CodeQL (MIT, active)
- https://github.com/joernio/joern — Joern CPG (Apache-2.0, active)
- https://github.com/DeusData/codebase-memory-mcp — Codebase Memory MCP (MIT, 29k stars)
- https://github.com/tirth8205/code-review-graph — Code Review Graph (MIT, 19k stars)
- https://github.com/vitali87/code-graph-rag — Code Graph RAG (MIT, 2.3k stars)
- https://github.com/bartolli/codanna — Codanna (Apache-2.0, 707 stars)
- https://github.com/CodeBendKit/codeseek — Codeseek (MIT, 614 stars)
- https://github.com/Cranot/roam-code — Roam Code (Apache-2.0, 495 stars)
- https://github.com/sdsrss/code-graph-mcp — Code Graph MCP (no license, 50 stars)
- https://github.com/ast-grep/ast-grep — ast-grep (MIT, 15k stars)
- https://github.com/sverweij/dependency-cruiser — dependency-cruiser (MIT, 6.9k stars)
- https://github.com/pahen/madge — madge (MIT, 10k stars, stale)
- https://github.com/thebjorn/pydeps — pydeps (BSD-2-Clause, 2.1k stars)
- https://github.com/CoatiSoftware/Sourcetrail — Sourcetrail (GPL-3.0, archived)
- https://github.com/glato/emerge — Emerge (stale, Oct 2024)
- https://github.com/MaibornWolff/codecharta — CodeCharta (BSD-3-Clause, 474 stars)
- https://github.com/fallow-rs/fallow — Fallow (MIT, 4k stars, TS/JS only)
- https://github.com/Ataraxy-Labs/sem — sem (Apache-2.0, 3k stars)
- https://github.com/Ataraxy-Labs/weave — weave (Apache-2.0, 1.2k stars)
- https://github.com/FalkorDB/code-graph — FalkorDB code-graph demo
- https://codescene.com — CodeScene (commercial)
- https://scitools.com — Understand/SciTools (commercial)
- https://sourcegraph.com — Sourcegraph (commercial, SCIP indexers open-source)