"""MCP stdio query surface for codeintel graphs."""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from codeintel import OUT_DIR, TARGETS_DIR
from codeintel.brief import BriefError, render_brief
from codeintel.config import ConfigError, load_target_by_name
from codeintel.core.graph import KuzuGraphStore
from codeintel.monitor import latest_snapshot_info
from codeintel.queries import CANNED, QueryError

WRITE_WORDS = re.compile(r"\b(CREATE|MERGE|SET|DELETE|DETACH|DROP|COPY|LOAD|ALTER|INSTALL|CALL)\b", re.I)

SCHEMA_NOTES = """Physical schema:
- Nodes are stored in one Kuzu node table: Node(id, kind, name, qname, path, line, props).
- Edges are stored in one relationship table: Edge(kind, props).
- Contract node and edge kinds are values in the `kind` property, not Kuzu labels.
- Query pattern: MATCH (a:Node)-[e:Edge]->(b:Node) WHERE a.kind = 'File' AND e.kind = 'IMPORTS' RETURN ...
- `props` is a JSON string for non-promoted properties; common node props are promoted to columns.
- This server exposes read-only queries only.
"""


def cli_run(args: argparse.Namespace) -> int:
    try:
        target = load_target_by_name(args.target, TARGETS_DIR)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    db = OUT_DIR / target.name / "graph.kuzu"
    if not db.exists():
        print(f"error: no graph at {db}; run `codeintel index {target.name}` first", file=sys.stderr)
        return 1

    mcp = build_server(target.name, db)
    mcp.run(transport="stdio")
    return 0


def build_server(target_name: str, db_path) -> FastMCP:
    mcp = FastMCP(f"codeintel-{target_name}")

    @mcp.tool()
    def query(cypher: str) -> list[dict[str, Any]]:
        """Run a guarded read-only Kuzu Cypher query against the target graph."""
        _guard_read_only(cypher)
        return _with_store(db_path, lambda store: store.query(cypher))

    @mcp.tool()
    def canned_query(name: str, arg: str | None = None) -> list[dict[str, Any]]:
        """Run one of codeintel.queries.CANNED by name."""
        fn = CANNED.get(name)
        if fn is None:
            raise ValueError(f"unknown canned query '{name}'. Available: {', '.join(sorted(CANNED))}")
        try:
            cypher = fn(arg)
        except QueryError as e:
            raise ValueError(str(e)) from e
        _guard_read_only(cypher)
        return _with_store(db_path, lambda store: store.query(cypher))

    @mcp.tool()
    def list_canned() -> list[str]:
        """List available canned query names."""
        return sorted(CANNED)

    @mcp.tool()
    def brief(target: str, about: str) -> str:
        """Return the same compact markdown context card as `codeintel brief`."""
        if target != target_name:
            raise ValueError(f"this MCP server is for target '{target_name}', not '{target}'")
        try:
            return render_brief(target_name, db_path, about)
        except BriefError as e:
            raise ValueError(str(e)) from e

    @mcp.tool()
    def stats() -> dict[str, Any]:
        """Return node and edge counts by kind, plus snapshot freshness."""
        graph_stats = _with_store(db_path, lambda store: store.stats())
        graph_stats["freshness"] = _freshness(target_name)
        return graph_stats

    @mcp.tool()
    def describe_schema() -> dict[str, Any]:
        """Return graph kinds and the Kuzu dialect notes needed by agents."""
        graph_stats = _with_store(db_path, lambda store: store.stats())
        return {
            "node_kinds": sorted(graph_stats["nodes"]),
            "edge_kinds": sorted(graph_stats["edges"]),
            "canned_queries": sorted(CANNED),
            "dialect_notes": SCHEMA_NOTES,
            "freshness": _freshness(target_name),
        }

    return mcp


def _with_store(db_path, fn):
    store = KuzuGraphStore(db_path)
    try:
        return fn(store)
    finally:
        store.close()


def _guard_read_only(cypher: str) -> None:
    stripped = cypher.strip()
    if not stripped:
        raise ValueError("query must not be empty")
    if ";" in stripped.rstrip(";"):
        raise ValueError("multiple statements are not allowed")
    if WRITE_WORDS.search(stripped):
        raise ValueError("only read-only MATCH/RETURN style queries are allowed")


def _freshness(target_name: str) -> dict[str, Any]:
    info = latest_snapshot_info(target_name)
    if info is None:
        return {
            "snapshot_path": None,
            "snapshot_timestamp": None,
            "age_hours": None,
            "stale": True,
            "hint": "run codeintel refresh",
        }
    age = info.get("age_hours")
    stale = age is None or age > 24
    return {
        "snapshot_path": info["path"],
        "snapshot_timestamp": info["timestamp"],
        "age_hours": None if age is None else round(float(age), 2),
        "stale": stale,
        "hint": "run codeintel refresh" if stale else None,
    }
