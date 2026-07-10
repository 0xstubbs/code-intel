# New Target

Add a YAML file under `targets/<name>.yaml`.

```yaml
name: example
root: /absolute/path/to/repo
exclude: [.git, .venv, __pycache__, node_modules, dist, build]
components:
  - {name: backend, path: services/api, lang: python, framework: fastapi}
  - {name: frontend, path: apps/web, lang: typescript, framework: nextjs}
extractors: [structure, python-api, nextjs, seams, dependencies, infra, tests, git-meta]
```

## Fields

- `name`: target name used by the CLI and under `out/<name>/`.
- `root`: absolute path to the codebase. codeintel must never write here.
- `exclude`: directory or file names skipped by extractors.
- `components`: logical source groups. `path` is relative to `root`.
- `extractors`: ordered extractor names. Unknown names are a config error.

## Components

Common `lang` values are `python`, `typescript`, `javascript`, and `mixed`. Common `framework` values are `fastapi` and `nextjs`; omit `framework` when no framework-specific extractor applies.

Use broad components when starting. Split later only when the graph needs clearer ownership boundaries.

## Extractors

- `structure`: files, modules, classes, functions, imports, inheritance, calls.
- `python-api`: FastAPI routes and route handlers.
- `nextjs`: Next.js pages and API proxies.
- `seams`: frontend/backend fetch and proxy chains.
- `dependencies`: package dependencies.
- `infra`: services and environment variables.
- `tests`: test nodes and `TESTED_BY` links.
- `git-meta`: file commit metadata.

For a small Python tools repository, this is usually enough:

```yaml
extractors: [structure, dependencies, infra, tests, git-meta]
```

After adding a target:

```bash
uv run codeintel index example
uv run codeintel query example files-by-component
```
