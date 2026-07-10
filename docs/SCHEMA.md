# Schema

The implemented v1 graph uses Kuzu with one node table and one relationship table.

## Physical Tables

`Node` columns:

- `id`: stable unique node ID.
- `kind`: contract node kind.
- `name`: promoted common prop.
- `qname`: promoted common prop.
- `path`: promoted repo-relative path.
- `line`: promoted source line.
- `props`: JSON string containing all emitted node props.

`Edge` columns:

- `kind`: contract edge kind.
- `props`: JSON string containing emitted edge props.

## Node Kinds

- `Target`: props `name`, `root`.
- `Component`: props `name`, `path`, `lang`, optional `framework`.
- `File`: props `name`, `path`, `component`, `lang`, plus git props `last_commit_date`, `last_commit_sha`, `commit_count_12mo`.
- `Module`: props `name`.
- `Class`: props `name`, `qname`, `path`, `line`.
- `Function`: props `name`, `qname`, `path`, `line`.
- `Route`: props `method`, `path`, `handler_path`, `line`.
- `Page`: props `route`, `path`, `layout`, `line`.
- `ApiProxy`: props `route`, `path`, `backend_path`, `rewrite`, `methods`, `line`.
- `Dependency`: props `name`, `source`, `locked_version`, `transitive`.
- `Service`: props `name`, `component`, `service_type`, `url`, `image`, `path`.
- `EnvVar`: props `name`.
- `Test`: props `name`, `path`, `line`.

Not every target emits every kind or every property.

## Edge Kinds

- `CONTAINS`: Target to Component to File hierarchy.
- `DEFINES`: File or Class defines Module, Class, or Function.
- `IMPORTS`: File imports File, Module, or Dependency.
- `CALLS`: Function calls Function. Props may include `line`.
- `INHERITS`: Class inherits Class.
- `EXPOSES`: Component exposes Route or Page.
- `HANDLED_BY`: Route handled by Function.
- `GUARDED_BY`: Route guarded by Function.
- `FETCHES`: Page, Component, or frontend Function fetches an ApiProxy or Route. Props may include `method`.
- `PROXIES`: ApiProxy proxies to Route.
- `DEPENDS_ON`: Component depends on Dependency. Props include `declared_version`, `locked_version`, `source`.
- `USES_SERVICE`: File or Component uses Service. Props may include `url`.
- `READS_ENV`: File reads EnvVar.
- `TESTED_BY`: Function, Class, or File tested by Test. Props may include `linkage`.

## IDs

Representative ID forms:

- `file:ui/backend/app/main.py`
- `fn:ui/backend/app/main.py::create_app`
- `class:ui/backend/app/models.py::Batch`
- `route:POST /api/upload`
- `page:/dashboard`
- `proxy:/api/images/[id]`
- `dep:pypi/fastapi`
- `svc:gcs`
- `env:LANGFUSE_HOST`
- `test:ui/backend/tests/test_upload.py::test_roundtrip`
- `component:backend`
