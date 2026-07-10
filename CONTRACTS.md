# codeintel — Shared Contracts (v1)

*Orchestrator-owned. Workers implement against this; propose changes via your final report, don't silently diverge.*

## Package & CLI
- Python ≥3.12, uv-managed (`uv run codeintel …`). Package: `codeintel/`.
- CLI subcommands (argparse or typer, keep deps lean):
  - `codeintel index <target>` — full pipeline: run all configured extractors → write facts → load graph.
  - `codeintel refresh <target>` — re-extract, diff vs last snapshot, write drift report.
  - `codeintel query <target> <name>|--cypher <q>` — canned or raw query.
  - `codeintel map <target>` — generate `out/<target>/map/ARCHITECTURE-MAP.md` + `viz.html`.
  - `codeintel targets` — list configured targets.
  - `codeintel mcp <target>` — serve the query surface over MCP (W5).

## Target config (`targets/<name>.yaml`)
```yaml
name: beancounter
root: /home/hermes/beancounter        # absolute; the platform NEVER writes under root
exclude: [node_modules, .venv, __pycache__, _archive, dogfood-output, extracted, logs, .git]
components:
  - {name: backend,  path: ui/backend,  lang: python,     framework: fastapi}
  - {name: frontend, path: ui/frontend, lang: typescript, framework: nextjs}
  - {name: tools,    path: ampro_tools, lang: python}
  - {name: e2e,      path: ui/e2e,      lang: javascript}
extractors: [structure, python-api, nextjs, seams, dependencies, infra, tests, git-meta]
```
Loader: `codeintel/config.py` → `TargetConfig` / `Component` dataclasses. Unknown extractor names error loudly.

## Facts (extractor output — the interchange format)
Each extractor writes `out/<target>/facts/<extractor>.json`:
```json
{"extractor": "python-api", "version": 1, "target": "beancounter",
 "nodes": [{"id": "…", "kind": "Route", "props": {…}}],
 "edges": [{"src": "…", "dst": "…", "kind": "HANDLED_BY", "props": {…}}]}
```
- Edges may reference nodes emitted by other extractors (resolved at load time; dangling refs are logged, not fatal).
- Props are flat JSON scalars. Every node with a source location carries `path` (repo-relative) and `line` where sensible.

### Stable node IDs
`<kind-prefix>:<repo-relative-path>[::<qualified-name>]`, e.g.
`file:ui/backend/app/main.py` · `fn:ui/backend/app/main.py::create_app` · `class:ui/backend/app/models.py::Batch` · `route:POST /api/upload` · `page:/dashboard` · `dep:pypi/fastapi` · `dep:npm/next` · `svc:gcs` · `env:LANGFUSE_HOST` · `test:ui/backend/tests/test_upload.py::test_roundtrip` · `component:backend`

### Node kinds (v1)
`Target, Component, File, Module, Class, Function, Route, Page, ApiProxy, Dependency, Service, EnvVar, Test`
(Methods are `Function` nodes with a `DEFINES` edge from their `Class`.)

### Edge kinds (v1)
| Kind | Meaning |
|---|---|
| `CONTAINS` | Target→Component→File hierarchy |
| `DEFINES` | File/Class → Module/Class/Function |
| `IMPORTS` | File → File/Module/Dependency |
| `CALLS` | Function → Function |
| `INHERITS` | Class → Class |
| `EXPOSES` | Component → Route/Page |
| `HANDLED_BY` | Route → Function |
| `GUARDED_BY` | Route → Function (auth/dependency guard) |
| `FETCHES` | Page/Component/Function (frontend) → ApiProxy/Route |
| `PROXIES` | ApiProxy → Route |
| `DEPENDS_ON` | Component → Dependency (props: declared_version, locked_version, source) |
| `USES_SERVICE` | File/Component → Service |
| `READS_ENV` | File → EnvVar |
| `TESTED_BY` | Function/Class/File → Test |
| `SIMILAR_TO` | Function → Function (props: score 0-1, method, loc); emitted once per pair with src.id < dst.id |

`git-meta` emits no nodes; it patches props onto existing File nodes: `last_commit_date`, `last_commit_sha`, `commit_count_12mo`.

## GraphStore (`codeintel/core/graph.py`)
Unified store = **Kuzu** embedded DB at `out/<target>/graph.kuzu` (writable, Cypher, no server).
```python
class GraphStore(Protocol):
    def load_facts(self, fact_files: list[Path]) -> LoadStats: ...
    def query(self, cypher: str) -> list[dict]: ...
    def stats(self) -> dict: ...   # node/edge counts by kind
```
The adopted structural core (CGC or codebase-memory-mcp, W1 bake-off) sits behind `codeintel/extractors/structure.py` — it *produces facts in the format above*, whatever it uses internally. That keeps the core swappable and the unified store ours.

## Extractor interface (`codeintel/extractors/base.py`)
```python
class Extractor(Protocol):
    name: str
    def extract(self, target: TargetConfig) -> Facts: ...  # reads target.root READ-ONLY
```
Registered in `codeintel/extractors/__init__.py: REGISTRY: dict[str, Extractor]`. Adding an analysis = one module + one registry line.

## Output layout
```
out/<target>/facts/*.json        # per-extractor facts (agent/tool-readable without a DB)
out/<target>/graph.kuzu/         # unified graph
out/<target>/map/ARCHITECTURE-MAP.md + viz.html
out/<target>/snapshots/<ts>.json # summary for drift diffing
out/<target>/drift/<ts>.md       # refresh reports
```

## Hard rules
1. **Never write under any target's `root`.** All output under `out/`. Never modify Beancounter.
2. Extractors must not import each other; they share only this contract.
3. Determinism: same input tree ⇒ same facts (sort outputs; no timestamps inside nodes/edges).
4. Failures degrade gracefully: one extractor failing must not abort `index`; log and continue.
