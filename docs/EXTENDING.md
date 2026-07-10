# Extending

An extractor is one module plus one registry slot.

## Contract

Implement the protocol from `codeintel/extractors/base.py`:

```python
class Extractor(Protocol):
    name: str
    def extract(self, target: TargetConfig) -> Facts: ...
```

Extractors read `target.root` and must not write there. They return a `Facts` object with deterministic node and edge dictionaries:

```json
{
  "extractor": "name",
  "version": 1,
  "target": "target-name",
  "nodes": [{"id": "file:path.py", "kind": "File", "props": {"path": "path.py"}}],
  "edges": [{"src": "component:app", "dst": "file:path.py", "kind": "CONTAINS", "props": {}}]
}
```

## Registry Slot

Add an optional slot in `codeintel/extractors/__init__.py`:

```python
_register_optional("my-extractor", "codeintel.extractors.my_extractor", "MyExtractor")
```

Then allow the name in `codeintel/config.py` if it is part of the public target YAML vocabulary.

## Determinism Rules

- Sort traversal results before emitting facts.
- Use stable node IDs from repo-relative paths and qualified names.
- Do not include timestamps or machine-local temporary paths in facts.
- Keep props flat JSON scalars.
- Let dangling edges be logged by the loader; do not invent placeholder nodes in a different extractor's domain.
- Fail gracefully: one extractor failure should not prevent other facts from being written.

## Graph Shape

The loader merges facts into one `Node` table and one `Edge` relationship table. New contract kinds do not require Kuzu DDL changes, but query examples and docs should be updated when a new kind becomes part of the supported surface.
