"""Extractor interface and the Facts interchange format (CONTRACTS.md).

Extractors read a target's tree READ-ONLY and produce Facts: flat node/edge
dicts. They must not import each other. Determinism rule: same input tree
=> byte-identical facts JSON (outputs sorted, no timestamps in the file —
see write_facts).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from codeintel.config import TargetConfig

log = logging.getLogger(__name__)

FACTS_VERSION = 1


@dataclass
class Facts:
    extractor: str
    target: str
    nodes: list[dict] = field(default_factory=list)
    edges: list[dict] = field(default_factory=list)
    version: int = FACTS_VERSION

    def add_node(self, id: str, kind: str, **props) -> None:
        self.nodes.append(
            {"id": id, "kind": kind, "props": {k: v for k, v in props.items() if v is not None}}
        )

    def add_edge(self, src: str, dst: str, kind: str, **props) -> None:
        self.edges.append(
            {"src": src, "dst": dst, "kind": kind,
             "props": {k: v for k, v in props.items() if v is not None}}
        )


@runtime_checkable
class Extractor(Protocol):
    name: str

    def extract(self, target: TargetConfig) -> Facts: ...


def _node_sort_key(n: dict):
    return (n.get("id", ""), n.get("kind", ""))


def _edge_sort_key(e: dict):
    return (e.get("kind", ""), e.get("src", ""), e.get("dst", ""),
            json.dumps(e.get("props", {}), sort_keys=True))


def normalize_facts(facts: Facts) -> Facts:
    """Sort and de-duplicate nodes/edges for deterministic output.

    Duplicate node ids within one extractor are merged (props union,
    first-writer wins per key); exactly-duplicate edges collapse to one.
    """
    merged: dict[str, dict] = {}
    for n in sorted(facts.nodes, key=_node_sort_key):
        prev = merged.get(n["id"])
        if prev is None:
            merged[n["id"]] = {"id": n["id"], "kind": n["kind"],
                               "props": dict(n.get("props", {}))}
        else:
            if prev["kind"] != n["kind"]:
                log.warning("node %s emitted with conflicting kinds %s/%s; keeping %s",
                            n["id"], prev["kind"], n["kind"], prev["kind"])
            for k, v in n.get("props", {}).items():
                prev["props"].setdefault(k, v)
    nodes = sorted(merged.values(), key=_node_sort_key)

    seen: set[str] = set()
    edges: list[dict] = []
    for e in sorted(facts.edges, key=_edge_sort_key):
        key = json.dumps(e, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        edges.append({"src": e["src"], "dst": e["dst"], "kind": e["kind"],
                      "props": dict(e.get("props", {}))})
    return Facts(extractor=facts.extractor, target=facts.target,
                 nodes=nodes, edges=edges, version=facts.version)


def write_facts(facts: Facts, out_path: Path) -> Facts:
    """Write facts JSON deterministically and return the normalized facts.

    Deviation from the CONTRACTS.md example header: no `generated_at` field.
    A wall-clock timestamp would break the byte-determinism rule (same input
    tree => identical facts file); refresh/drift tooling should use file
    mtimes or snapshot metadata instead.
    """
    norm = normalize_facts(facts)
    doc = {
        "extractor": norm.extractor,
        "version": norm.version,
        "target": norm.target,
        "nodes": norm.nodes,
        "edges": norm.edges,
    }
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(doc, indent=1, sort_keys=True) + "\n")
    return norm
