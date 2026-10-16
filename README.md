# codeintel

codeintel is a uv-managed Python 3.12 code-intelligence platform. It reads configured repositories without modifying them, extracts deterministic fact JSON, and loads those facts into an embedded Kuzu graph for canned queries, raw Cypher, drift checks, maps, and MCP access.

Outputs live under `out/<target>/`; target repositories stay read-only.

## Install (system-wide)

```bash
uv tool install --editable /home/hermes/code-intel
```

Puts `codeintel` on PATH (`~/.local/bin`), runnable from any directory. Editable: platform
changes take effect without reinstalling. Targets can be referenced by **name or root path**:

```bash
codeintel findings ~/beancounter
codeintel brief ~/beancounter --about classify_scan_type
```

## Quickstart

```bash
codeintel targets
codeintel index beancounter          # or: codeintel index ~/beancounter
codeintel query beancounter files-by-component
codeintel refresh beancounter
codeintel findings beancounter
codeintel mcp beancounter
```

(Inside the repo, `uv run codeintel …` works too and always uses the project venv.)

The query graph uses one `Node` table and one `Edge` relationship table. Contract kinds such as `File`, `Route`, `Dependency`, `CALLS`, and `HANDLED_BY` are stored in the `kind` property.

## Maintained project documentation

Before development or independent review, read this README for the project
workflow, [PLAN.md](PLAN.md) for requirements and design decisions,
[CONTRACTS.md](CONTRACTS.md) for shared interfaces and invariants, and the affected
[usage](docs/USAGE.md), [extension](docs/EXTENDING.md) or [schema](docs/SCHEMA.md) guide.
These existing documents are the maintained repository documentation; an issue
tracks work and links the relevant documents rather than replacing them.

Update affected workflows, requirements, contracts and design decisions in the
same change as the behavior they describe. Record a changed decision and its
reason in the existing plan or affected contract, preserving dated evidence and
explicitly separating proposed, implemented and exercised behavior. Independent
review checks that documentation matches the exact change and retains intended
behavior that has not shipped. Reuse these documents instead of adding another
specification or task backlog.

Historical shipped labels, paths and command examples require confirmation
against the selected source/runtime before use. Recovered local branches, draft
designs, indexes and qualification receipts remain separate evidence until they
are explicitly reviewed and integrated. Publishing these instructions does not
install a runtime, connect MCP, refresh a graph or establish that a loader has
included the complete text in an existing session.

## Docs

- [Usage](docs/USAGE.md): install, index, refresh, and validated Cypher examples.
- [New Target](docs/NEW-TARGET.md): target YAML schema and component guidance.
- [Extending](docs/EXTENDING.md): adding extractors and deterministic fact rules.
- [Schema](docs/SCHEMA.md): implemented node and edge kinds.

## v1 Status

The v1 surface is intentionally thin: extractors write facts, the loader rebuilds a local Kuzu graph, and query tools read that graph. The richer extractors are optional registry modules, so a target can still index when a specific extractor is absent or fails. Drift snapshots are written by `refresh`; facts themselves remain timestamp-free for deterministic diffs.
