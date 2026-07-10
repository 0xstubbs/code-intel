"""Canned queries over the unified graph.

Each canned query is a function (arg: str | None) -> cypher string against
the physical schema (Node/Edge tables, contract kinds as `kind` props).
"""

from __future__ import annotations


class QueryError(Exception):
    pass


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _norm_file_id(arg: str) -> str:
    return arg if arg.startswith("file:") else f"file:{arg}"


def files_by_component(arg: str | None) -> str:
    return (
        "MATCH (c:Node)-[e:Edge]->(f:Node) "
        "WHERE c.kind = 'Component' AND e.kind = 'CONTAINS' AND f.kind = 'File' "
        "RETURN c.name AS component, count(f) AS files ORDER BY component"
    )


def functions_in(arg: str | None) -> str:
    if not arg:
        raise QueryError("functions-in requires a repo-relative file path argument")
    return (
        f"MATCH (n:Node) WHERE n.kind = 'Function' AND n.path = '{_esc(arg)}' "
        "RETURN n.id AS id, n.qname AS qname, n.line AS line ORDER BY line, id"
    )


def callers_of(arg: str | None) -> str:
    """Callers of a function, by full node id, qualified name, or bare name."""
    if not arg:
        raise QueryError("callers-of requires a function id, qualified name, or name")
    q = _esc(arg)
    return (
        "MATCH (a:Node)-[e:Edge]->(b:Node) "
        f"WHERE e.kind = 'CALLS' AND (b.id = '{q}' OR b.qname = '{q}' OR b.name = '{q}') "
        "RETURN a.id AS caller, a.path AS path, a.line AS line, b.id AS callee "
        "ORDER BY caller, callee"
    )


def calls_from(arg: str | None) -> str:
    if not arg:
        raise QueryError("calls-from requires a function id, qualified name, or name")
    q = _esc(arg)
    return (
        "MATCH (a:Node)-[e:Edge]->(b:Node) "
        f"WHERE e.kind = 'CALLS' AND (a.id = '{q}' OR a.qname = '{q}' OR a.name = '{q}') "
        "RETURN b.id AS callee, b.path AS path, b.line AS line ORDER BY callee"
    )


def imports_of(arg: str | None) -> str:
    if not arg:
        raise QueryError("imports-of requires a repo-relative file path argument")
    fid = _esc(_norm_file_id(arg))
    return (
        "MATCH (f:Node)-[e:Edge]->(m:Node) "
        f"WHERE e.kind = 'IMPORTS' AND f.id = '{fid}' "
        "RETURN m.id AS imported, m.kind AS kind ORDER BY imported"
    )


def imported_by(arg: str | None) -> str:
    if not arg:
        raise QueryError("imported-by requires a repo-relative file path argument")
    fid = _esc(_norm_file_id(arg))
    return (
        "MATCH (f:Node)-[e:Edge]->(m:Node) "
        f"WHERE e.kind = 'IMPORTS' AND m.id = '{fid}' "
        "RETURN f.id AS importer ORDER BY importer"
    )


def classes_in(arg: str | None) -> str:
    if not arg:
        raise QueryError("classes-in requires a repo-relative file path argument")
    return (
        f"MATCH (n:Node) WHERE n.kind = 'Class' AND n.path = '{_esc(arg)}' "
        "RETURN n.id AS id, n.qname AS qname, n.line AS line ORDER BY line, id"
    )


CANNED = {
    "files-by-component": files_by_component,
    "functions-in": functions_in,
    "classes-in": classes_in,
    "callers-of": callers_of,
    "calls-from": calls_from,
    "imports-of": imports_of,
    "imported-by": imported_by,
}
