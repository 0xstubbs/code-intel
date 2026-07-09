# Phase 0a — Beancounter Inventory (target #1)

*What a code-intelligence tool must handle to map this codebase. Snapshot 2026-07-09.*

## Languages & versions
| Layer | Language | Version | Volume |
|---|---|---|---|
| Backend | Python | ≥3.11 (uv-managed) | ~104 .py files (app + ampro_tools) |
| Frontend | TypeScript/TSX | TS 5, React 19 | ~51 .ts/.tsx files |
| E2E / scripts | JS (.mjs), Bash | node, bash | run-journeys.mjs harness, start.sh |
| **Total product code** | | | **~41k LOC** (excl. node_modules/venv) |

## Frameworks & notable constraints
- **Backend:** FastAPI + uvicorn (`--reload`, port **8001**), psycopg3 → **Postgres**; separate **Neon** Postgres (AD database); Google Cloud Storage; Langfuse (US cloud) observability; vision via Gemini/OpenRouter providers.
- **Frontend:** **Next.js 16 — a modified build**; `ui/frontend/AGENTS.md` warns its APIs may differ from public Next.js (docs live in `node_modules/next/dist/docs/`). App Router, Tailwind 4, tldraw 5, lucide-react. Port **3000**; API calls proxied same-origin via `app/api/`.
- **No ORM migrations** (no alembic); schema managed in code (`app/db.py`).
- **Git dependency:** `ampro-core` pinned to `ssh://git@github.com/0xstubbs/ampro.git#subdirectory=ampro-core` (shared AD-matching lib) — SBOM tooling must handle a git+ssh source.

## Package managers & lockfiles
| Manifest | Lockfile | Manager |
|---|---|---|
| `pyproject.toml` (root, `ampro-tools` pkg) | `uv.lock` | uv |
| `ui/backend/pyproject.toml` (`logbook-web-backend`) | `ui/backend/uv.lock` | uv |
| `ui/frontend/package.json` | `package-lock.json` | npm |
| `ui/e2e/package.json` | `package-lock.json` | npm |

## Entry points
- `start.sh` — launches backend (:8001) + frontend (:3000), logs to `logs/`.
- `ui/backend/app/main.py` — FastAPI app; **16 routers** in `app/routers/` (batches, extraction(s), images, logbook, openrouter, packages, profiles, reference(+context), traces, upload, workflow, browse_root, _guards).
- `ui/frontend/app/` — App Router pages: `batch`, `dashboard`, `documents`, `pipeline`, `scratchpad`, `upload`, `workflows`, plus `app/api/` proxy routes.
- CLI: `ampro-tools` / `logbook-pipeline` console scripts (root package).

## Backend module boundaries (bounded contexts)
- **Digitization pipeline:** `ingestion/` → `preprocessing.py`/`photo_preprocess.py` → `extraction/` + `vision/` → `assembly/` → `packaging.py` (.logpk).
- **Compliance:** ampro-core AD matching against Neon (`neon_db.py`, `refdb.py`, reference routers).
- **Observability:** `langfuse_client.py`, `langfuse_middleware.py`, `extraction_traces`.
- **Support:** `storage.py` (GCS), `scrapers/`, `embeddings.py`, `jobs.py`.

## Infra / config surface
- Dockerfiles: `ui/backend/Dockerfile`, `ui/frontend/Dockerfile` (no docker-compose found).
- Env/config: `ui/backend/.env` (gitignored; model + keys), `pydantic-settings`, `~/.env` for shared keys.
- External services: local Postgres, Neon, GCS, Langfuse US, Gemini/OpenRouter APIs, self-hosted Firecrawl (:3002, unrelated to app runtime).
- Docs corpus: `docs/` (product spec, plans, BUGLOG), `evals/`, `_archive/`.

## Implications for the platform
1. Must parse **Python + TypeScript/TSX** first-class; Bash/Dockerfile/JSON/YAML/TOML as config surface.
2. Must model **cross-language seams**: Next.js page/component → same-origin `/api` proxy → FastAPI route → DB/GCS/vision provider.
3. Must read **uv and npm lockfiles** (incl. git+ssh deps) for the dependency layer.
4. Multiple sub-projects in one repo (root pkg, backend, frontend, e2e) → target config needs per-repo *components*, not one root.
5. The modified Next.js means route conventions should be extracted from this repo's actual code, not assumed from stock Next.js — favor pattern-based extraction over framework-version assumptions.
