"""Kuzu-backed unified GraphStore (CONTRACTS.md "GraphStore").

Physical schema: one node table `Node` and one rel table `Edge`; the
contract's node/edge *kinds* are `kind` properties, so extractors can add
new kinds without DDL churn. Frequently-queried props are promoted to
columns (name, qname, path, line); the full props dict is kept as a JSON
string in `props`.

Loading is a full rebuild: load_facts() recreates the DB directory, merges
all fact files in memory (later files patch props onto existing nodes —
e.g. git-meta), drops edges whose endpoints don't exist anywhere in the
merged set (logged, not fatal), then bulk-loads via CSV COPY.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import kuzu

log = logging.getLogger(__name__)

PROMOTED_NODE_PROPS = ("name", "qname", "path", "line")


@dataclass
class LoadStats:
    fact_files: int = 0
    nodes: int = 0
    edges: int = 0
    dangling_edges_dropped: int = 0
    nodes_by_kind: dict[str, int] = field(default_factory=dict)
    edges_by_kind: dict[str, int] = field(default_factory=dict)


class GraphStore(Protocol):
    def load_facts(self, fact_files: list[Path]) -> LoadStats: ...
    def query(self, cypher: str) -> list[dict]: ...
    def stats(self) -> dict: ...


class KuzuGraphStore:
    """GraphStore implementation over an embedded Kuzu database directory."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._db: kuzu.Database | None = None
        self._conn: kuzu.Connection | None = None

    # -- connection ---------------------------------------------------------

    def _connect(self) -> kuzu.Connection:
        if self._conn is None:
            self._db = kuzu.Database(str(self.db_path))
            self._conn = kuzu.Connection(self._db)
        return self._conn

    def close(self) -> None:
        self._conn = None
        if self._db is not None:
            self._db.close()
            self._db = None

    # -- loading ------------------------------------------------------------

    def load_facts(self, fact_files: list[Path]) -> LoadStats:
        stats = LoadStats()
        nodes: dict[str, dict] = {}  # id -> {kind, props}
        edges: list[dict] = []

        for path in sorted(Path(p) for p in fact_files):
            try:
                doc = json.loads(Path(path).read_text())
            except (OSError, json.JSONDecodeError) as e:
                log.error("skipping unreadable fact file %s: %s", path, e)
                continue
            stats.fact_files += 1
            for n in doc.get("nodes", []):
                nid, kind = n["id"], n["kind"]
                props = dict(n.get("props", {}))
                prev = nodes.get(nid)
                if prev is None:
                    nodes[nid] = {"kind": kind, "props": props}
                else:
                    if prev["kind"] != kind:
                        log.warning(
                            "node %s: kind conflict across extractors (%s vs %s); keeping %s",
                            nid, prev["kind"], kind, prev["kind"])
                    prev["props"].update(props)  # later fact files patch props (git-meta)
            for e in doc.get("edges", []):
                edges.append({"src": e["src"], "dst": e["dst"], "kind": e["kind"],
                              "props": dict(e.get("props", {}))})

        kept_edges = []
        for e in edges:
            if e["src"] in nodes and e["dst"] in nodes:
                kept_edges.append(e)
            else:
                stats.dangling_edges_dropped += 1
                missing = [x for x in (e["src"], e["dst"]) if x not in nodes]
                log.info("dropping dangling %s edge %s -> %s (missing: %s)",
                         e["kind"], e["src"], e["dst"], ", ".join(missing))
        if stats.dangling_edges_dropped:
            log.warning("dropped %d dangling edges (endpoints not emitted by any extractor)",
                        stats.dangling_edges_dropped)

        self._rebuild(nodes, kept_edges)

        stats.nodes = len(nodes)
        stats.edges = len(kept_edges)
        for n in nodes.values():
            stats.nodes_by_kind[n["kind"]] = stats.nodes_by_kind.get(n["kind"], 0) + 1
        for e in kept_edges:
            stats.edges_by_kind[e["kind"]] = stats.edges_by_kind.get(e["kind"], 0) + 1
        return stats

    def _rebuild(self, nodes: dict[str, dict], edges: list[dict]) -> None:
        self.close()
        if self.db_path.exists():
            if self.db_path.is_dir():
                shutil.rmtree(self.db_path)
            else:
                self.db_path.unlink()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        conn.execute(
            "CREATE NODE TABLE Node(id STRING, kind STRING, name STRING, qname STRING, "
            "path STRING, line INT64, props STRING, PRIMARY KEY(id))")
        conn.execute("CREATE REL TABLE Edge(FROM Node TO Node, kind STRING, props STRING)")

        with tempfile.TemporaryDirectory(prefix="codeintel-load-") as tmp:
            nodes_csv = Path(tmp) / "nodes.csv"
            edges_csv = Path(tmp) / "edges.csv"
            with nodes_csv.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["id", "kind", "name", "qname", "path", "line", "props"])
                for nid in sorted(nodes):
                    n = nodes[nid]
                    p = n["props"]
                    line = p.get("line")
                    w.writerow([
                        nid, n["kind"],
                        p.get("name", ""), p.get("qname", ""), p.get("path", ""),
                        "" if line is None else int(line),
                        json.dumps(p, sort_keys=True),
                    ])
            with edges_csv.open("w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["from", "to", "kind", "props"])
                for e in edges:
                    w.writerow([e["src"], e["dst"], e["kind"],
                                json.dumps(e["props"], sort_keys=True)])
            copy_opts = "(HEADER=true, QUOTE='\"', ESCAPE='\"')"
            conn.execute(f'COPY Node FROM "{nodes_csv}" {copy_opts}')
            if edges:
                conn.execute(f'COPY Edge FROM "{edges_csv}" {copy_opts}')

    # -- querying -----------------------------------------------------------

    def query(self, cypher: str) -> list[dict]:
        conn = self._connect()
        result = conn.execute(cypher)
        cols = result.get_column_names()
        rows: list[dict] = []
        while result.has_next():
            vals = result.get_next()
            rows.append({c: _jsonable(v) for c, v in zip(cols, vals)})
        return rows

    def stats(self) -> dict:
        nodes = self.query(
            "MATCH (n:Node) RETURN n.kind AS kind, count(*) AS count ORDER BY kind")
        edges = self.query(
            "MATCH ()-[e:Edge]->() RETURN e.kind AS kind, count(*) AS count ORDER BY kind")
        return {
            "nodes": {r["kind"]: r["count"] for r in nodes},
            "edges": {r["kind"]: r["count"] for r in edges},
            "total_nodes": sum(r["count"] for r in nodes),
            "total_edges": sum(r["count"] for r in edges),
        }


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    return str(v)
