# Usage

Run commands from the repository root with `uv run`.

## Install

```bash
uv sync
uv run codeintel targets
```

Targets are never modified. Extracted facts, graphs, snapshots, drift reports, and maps are written under `out/<target>/`.

## Index

```bash
uv run codeintel index beancounter
uv run codeintel refresh beancounter
uv run codeintel query beancounter files-by-component
```

`index` runs configured extractors and rebuilds `out/<target>/graph.kuzu`. `refresh` does the same full extraction/load cycle and also writes:

- `out/<target>/snapshots/<UTC-ts>.json`
- `out/<target>/drift/<UTC-ts>.md`

## Query Dialect

The Kuzu graph has one node table and one relationship table:

- `Node(id, kind, name, qname, path, line, props)`
- `Edge(kind, props)`

Contract kinds are values in `kind`, not Kuzu labels. Use `MATCH (a:Node)-[e:Edge]->(b:Node)` and filter with `a.kind = 'File'`, `e.kind = 'IMPORTS'`, etc. Only `name`, `qname`, `path`, and `line` are promoted to direct columns; other properties are available in the JSON string column `props`.

## Briefs

`brief` prints a compact markdown context card from the graph. It auto-detects whether `--about` is a route, repo-relative file path, symbol name/qname, or component. Ambiguous symbol or substring matches exit `3` and list candidates.

```bash
uv run codeintel brief beancounter --about "POST /api/v1/batches/{batch_id}/preprocess"
uv run codeintel brief beancounter --about ui/backend/app/photo_preprocess.py
uv run codeintel brief beancounter --about classify_scan_type
```

Cards include a `FRESHNESS` line from the latest `out/<target>/snapshots/*` file. If the snapshot is older than 24 hours or missing, the card tells you to run `codeintel refresh`.

## Findings

`findings` writes a graph-backed code-health report:

```bash
uv run codeintel findings beancounter
```

Outputs:

- `out/<target>/findings/FINDINGS.md`
- `out/<target>/findings/findings.json`

The JSON file is deterministic and uses this stable item schema:

```json
{
  "id": "F0001",
  "category": "dead-code-candidates",
  "severity": "info|review|action",
  "confidence": "high|medium|low",
  "title": "short finding title",
  "detail": "why the graph reported it",
  "evidence": ["path:line [node-id]"]
}
```

Implemented categories include dead-code candidates, stale-but-wired files, untested surface, unguarded mutating routes, duplication clusters, production-orphaned modules, and env/config surface. The report is graph-only: dynamic calls, external consumers, registries, templates, and unresolved imports can still require manual review.

## Wiki

`wiki` writes a graph-grounded narrative wiki under `out/<target>/wiki/`:

```bash
uv run codeintel wiki beancounter
uv run codeintel wiki beancounter --page backend.md --force
```

It uses OpenRouter. Set `OPENROUTER_API_KEY` (or `OPEN_ROUTER_API_KEY`) in the environment or in `/home/hermes/.env`; the default model is `google/gemini-3.5-flash`, overridable with `CODEINTEL_WIKI_MODEL` or `--model`.

Pages are assembled from context packs built only from codeintel facts, graph edges, snapshots, and findings. Target source code is not sent to the model. The generated pages are:

- `index.md`
- `architecture.md`
- one `<component>.md` page per configured component
- `seams.md`
- `getting-started.md`

`architecture.md` includes a Mermaid component/seams diagram generated deterministically in Python from graph edges; the model writes only prose around it.

Regeneration is drift-aware. `out/<target>/wiki/.state.json` maps each page to the SHA-256 of its context pack, model, and graph snapshot timestamp. Re-running skips unchanged pages with `up to date`; `--force` regenerates all selected pages. `--page <file>` narrows work to one page.

Honesty boundary: `wiki/*.md` prose is LLM output and is not covered by the byte-determinism guarantee. The `.state.json` input hashes are deterministic for a fixed graph/facts snapshot.

## Sample Queries

What handles a route:

```cypher
MATCH (r:Node)-[e:Edge]->(f:Node)
WHERE r.kind='Route' AND e.kind='HANDLED_BY'
RETURN r.id AS route, r.props AS route_props, f.path AS file, f.qname AS handler
ORDER BY route LIMIT 5
```

Full seam chain from frontend fetch to backend handler:

```cypher
MATCH (p:Node)-[f:Edge]->(a:Node)-[pr:Edge]->(r:Node)-[h:Edge]->(fn:Node)
WHERE f.kind='FETCHES' AND pr.kind='PROXIES' AND h.kind='HANDLED_BY'
RETURN p.kind AS source_kind, p.path AS source_path, a.id AS api_proxy, r.id AS route, fn.qname AS handler
LIMIT 5
```

What depends on a package:

```cypher
MATCH (c:Node)-[e:Edge]->(d:Node)
WHERE c.kind='Component' AND e.kind='DEPENDS_ON' AND d.kind='Dependency' AND d.name='fastapi'
RETURN c.name AS component, d.id AS dependency, e.props AS edge_props
LIMIT 5
```

Functions with no inbound `CALLS`:

```cypher
MATCH (f:Node)
WHERE f.kind='Function'
  AND NOT EXISTS { MATCH (:Node)-[e:Edge]->(f) WHERE e.kind='CALLS' }
RETURN f.id AS function, f.path AS path
ORDER BY function LIMIT 5
```

Files with no `TESTED_BY` edge:

```cypher
MATCH (f:Node)
WHERE f.kind='File'
  AND NOT EXISTS { MATCH (f)-[e:Edge]->(:Node) WHERE e.kind='TESTED_BY' }
RETURN f.path AS file
ORDER BY file LIMIT 5
```

Routes and their guards:

```cypher
MATCH (r:Node)-[g:Edge]->(fn:Node)
WHERE r.kind='Route' AND g.kind='GUARDED_BY'
RETURN r.id AS route, r.props AS route_props, fn.qname AS guard
ORDER BY route LIMIT 5
```

Files by staleness via `last_commit_date` in `props`:

```cypher
MATCH (f:Node)
WHERE f.kind='File'
RETURN f.path AS file, f.props AS props
ORDER BY f.props LIMIT 5
```

Environment variables by file:

```cypher
MATCH (f:Node)-[e:Edge]->(v:Node)
WHERE f.kind='File' AND e.kind='READS_ENV' AND v.kind='EnvVar'
RETURN f.path AS file, v.name AS env_var
ORDER BY file, env_var LIMIT 5
```

Services by component:

```cypher
MATCH (c:Node)-[cf:Edge]->(f:Node)-[us:Edge]->(svc:Node)
WHERE c.kind='Component' AND cf.kind='CONTAINS'
  AND f.kind='File' AND us.kind='USES_SERVICE' AND svc.kind='Service'
RETURN c.name AS component, svc.name AS service, svc.props AS service_props
ORDER BY component, service LIMIT 5
```

Impact: what frontend fetches hit routes handled by a given backend file:

```cypher
MATCH (p:Node)-[fe:Edge]->(a:Node)-[pr:Edge]->(r:Node)-[hb:Edge]->(fn:Node)
WHERE fe.kind='FETCHES' AND pr.kind='PROXIES' AND hb.kind='HANDLED_BY'
  AND fn.path='ui/backend/app/routers/images.py'
RETURN p.id AS fetcher, a.id AS api_proxy, r.id AS route
ORDER BY fetcher LIMIT 5
```
