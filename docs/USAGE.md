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
