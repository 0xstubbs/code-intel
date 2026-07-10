# codeintel

codeintel is a uv-managed Python 3.12 code-intelligence platform. It reads configured repositories without modifying them, extracts deterministic fact JSON, and loads those facts into an embedded Kuzu graph for canned queries, raw Cypher, drift checks, maps, and MCP access.

Outputs live under `out/<target>/`; target repositories stay read-only.

## Quickstart

```bash
uv sync
uv run codeintel targets
uv run codeintel index beancounter
uv run codeintel query beancounter files-by-component
uv run codeintel refresh beancounter
uv run codeintel mcp beancounter
```

The query graph uses one `Node` table and one `Edge` relationship table. Contract kinds such as `File`, `Route`, `Dependency`, `CALLS`, and `HANDLED_BY` are stored in the `kind` property.

## Docs

- [Usage](docs/USAGE.md): install, index, refresh, and validated Cypher examples.
- [New Target](docs/NEW-TARGET.md): target YAML schema and component guidance.
- [Extending](docs/EXTENDING.md): adding extractors and deterministic fact rules.
- [Schema](docs/SCHEMA.md): implemented node and edge kinds.

## v1 Status

The v1 surface is intentionally thin: extractors write facts, the loader rebuilds a local Kuzu graph, and query tools read that graph. The richer extractors are optional registry modules, so a target can still index when a specific extractor is absent or fails. Drift snapshots are written by `refresh`; facts themselves remain timestamp-free for deterministic diffs.
