"""Refresh pipeline and drift snapshots for codeintel targets."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from codeintel import OUT_DIR, TARGETS_DIR
from codeintel.config import ConfigError, TargetConfig, load_target_by_name
from codeintel.core.graph import KuzuGraphStore
from codeintel.extractors import REGISTRY, write_facts

log = logging.getLogger("codeintel.monitor")

HEADLINE_KINDS = ("Route", "Page", "ApiProxy", "Dependency", "Service", "EnvVar", "Component")


def cli_run(args: argparse.Namespace) -> int:
    target = _load_target_or_die(args.target)
    out = OUT_DIR / target.name
    refresh = run_refresh(target, out)
    print(f"snapshot: {refresh['snapshot_path']}")
    print(f"drift: {refresh['drift_path']}")
    print(refresh["summary"])
    if getattr(args, "check", False) and refresh["has_drift"]:
        return 1
    return 0


def latest_snapshot_info(target: str) -> dict[str, Any] | None:
    """Return metadata for the newest drift snapshot for a target, if any."""
    snapshots_dir = OUT_DIR / target / "snapshots"
    snapshots = sorted(snapshots_dir.glob("*.json"))
    if not snapshots:
        return None
    path = snapshots[-1]
    timestamp = path.stem
    try:
        doc = json.loads(path.read_text())
        timestamp = str(doc.get("generated_at") or timestamp)
    except (OSError, json.JSONDecodeError):
        pass
    generated = _parse_snapshot_ts(timestamp)
    age_hours = None
    if generated is not None:
        age_hours = max(0.0, (datetime.now(timezone.utc) - generated).total_seconds() / 3600)
    return {
        "path": str(path),
        "timestamp": timestamp,
        "age_hours": age_hours,
    }


def run_refresh(target: TargetConfig, out: Path) -> dict[str, Any]:
    facts_dir = out / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for name in target.extractors:
        extractor = REGISTRY.get(name)
        if extractor is None:
            log.warning("extractor '%s' configured but not in REGISTRY; skipping", name)
            continue
        t0 = time.time()
        try:
            facts = extractor.extract(target)
        except Exception:
            failures += 1
            log.exception("extractor '%s' failed; continuing", name)
            continue
        norm = write_facts(facts, facts_dir / f"{name}.json")
        log.info("extractor '%s': %d nodes, %d edges in %.1fs",
                 name, len(norm.nodes), len(norm.edges), time.time() - t0)

    fact_files = sorted(facts_dir.glob("*.json"))
    if not fact_files:
        print("error: no fact files produced; nothing to load", file=sys.stderr)
        raise SystemExit(1)

    store = KuzuGraphStore(out / "graph.kuzu")
    load = store.load_facts(fact_files)
    stats = store.stats()
    store.close()
    log.info("graph loaded: %d nodes, %d edges from %d fact files (%d dangling edges dropped)",
             load.nodes, load.edges, load.fact_files, load.dangling_edges_dropped)
    if failures and load.nodes == 0:
        raise SystemExit(1)

    snapshot = build_snapshot(target, fact_files, stats)
    snapshot_path = write_snapshot(out, snapshot)
    snapshots = sorted((out / "snapshots").glob("*.json"))
    previous = snapshots[-2] if len(snapshots) >= 2 else None
    drift = compare_snapshots(_read_json(previous) if previous else None, snapshot)
    drift_path = write_drift_report(out, snapshot["generated_at"], drift, previous, snapshot_path)
    return {
        "snapshot_path": str(snapshot_path),
        "drift_path": str(drift_path),
        "has_drift": drift["has_drift"],
        "summary": drift["summary"],
    }


def build_snapshot(target: TargetConfig, fact_files: list[Path], stats: dict[str, Any]) -> dict[str, Any]:
    merged_nodes: dict[str, dict[str, Any]] = {}
    for path in fact_files:
        doc = json.loads(path.read_text())
        for node in doc.get("nodes", []):
            current = merged_nodes.get(node["id"])
            if current is None:
                merged_nodes[node["id"]] = {"kind": node["kind"], "props": dict(node.get("props", {}))}
            else:
                current["props"].update(dict(node.get("props", {})))

    headline_ids: dict[str, list[str]] = {kind: [] for kind in HEADLINE_KINDS}
    headline_fingerprints: dict[str, dict[str, str]] = {kind: {} for kind in HEADLINE_KINDS}
    node_ids_by_kind: dict[str, list[str]] = {}
    node_fingerprints_by_kind: dict[str, dict[str, str]] = {}
    for node_id, node in sorted(merged_nodes.items()):
        kind = node["kind"]
        node_ids_by_kind.setdefault(kind, []).append(node_id)
        node_fingerprints_by_kind.setdefault(kind, {})[node_id] = _digest_json(node)
        if kind not in headline_ids:
            continue
        headline_ids[kind].append(node_id)
        headline_fingerprints[kind][node_id] = _digest_json(node)

    return {
        "target": target.name,
        "generated_at": _utc_ts(),
        "extractors": {
            p.stem: {"fact_file": p.name, "sha256": _sha256(p)}
            for p in sorted(fact_files)
        },
        "counts": {
            "nodes": dict(sorted(stats.get("nodes", {}).items())),
            "edges": dict(sorted(stats.get("edges", {}).items())),
            "total_nodes": stats.get("total_nodes", 0),
            "total_edges": stats.get("total_edges", 0),
        },
        "headline_ids": {k: sorted(v) for k, v in headline_ids.items()},
        "headline_fingerprints": headline_fingerprints,
        "node_ids_by_kind": {k: sorted(v) for k, v in sorted(node_ids_by_kind.items())},
        "node_fingerprints_by_kind": node_fingerprints_by_kind,
    }


def write_snapshot(out: Path, snapshot: dict[str, Any]) -> Path:
    path = out / "snapshots" / f"{snapshot['generated_at']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    return path


def compare_snapshots(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    if previous is None:
        return {
            "has_drift": False,
            "summary": "no previous snapshot; baseline recorded",
            "headline": {},
            "other_nodes": {},
            "count_deltas": {"nodes": {}, "edges": {}},
            "extractor_hash_changes": {},
        }

    headline: dict[str, Any] = {}
    for kind in HEADLINE_KINDS:
        prev_ids = set(previous.get("headline_ids", {}).get(kind, []))
        curr_ids = set(current.get("headline_ids", {}).get(kind, []))
        prev_fp = previous.get("headline_fingerprints", {}).get(kind, {})
        curr_fp = current.get("headline_fingerprints", {}).get(kind, {})
        changed = sorted(i for i in prev_ids & curr_ids if prev_fp.get(i) != curr_fp.get(i))
        added = sorted(curr_ids - prev_ids)
        removed = sorted(prev_ids - curr_ids)
        headline[kind] = {"added": added, "removed": removed, "changed": changed}

    count_deltas = {
        "nodes": _count_deltas(previous["counts"]["nodes"], current["counts"]["nodes"]),
        "edges": _count_deltas(previous["counts"]["edges"], current["counts"]["edges"]),
    }
    prev_extractors = previous.get("extractors", {})
    curr_extractors = current.get("extractors", {})
    names = sorted(set(prev_extractors) | set(curr_extractors))
    extractor_changes = {
        name: {
            "before": prev_extractors.get(name, {}).get("sha256"),
            "after": curr_extractors.get(name, {}).get("sha256"),
        }
        for name in names
        if prev_extractors.get(name, {}).get("sha256") != curr_extractors.get(name, {}).get("sha256")
    }
    other_nodes: dict[str, Any] = {}
    all_kinds = sorted(
        (set(previous.get("node_ids_by_kind", {})) | set(current.get("node_ids_by_kind", {})))
        - set(HEADLINE_KINDS)
    )
    for kind in all_kinds:
        prev_ids = set(previous.get("node_ids_by_kind", {}).get(kind, []))
        curr_ids = set(current.get("node_ids_by_kind", {}).get(kind, []))
        prev_fp = previous.get("node_fingerprints_by_kind", {}).get(kind, {})
        curr_fp = current.get("node_fingerprints_by_kind", {}).get(kind, {})
        changed = sorted(i for i in prev_ids & curr_ids if prev_fp.get(i) != curr_fp.get(i))
        added = sorted(curr_ids - prev_ids)
        removed = sorted(prev_ids - curr_ids)
        if added or removed or changed:
            other_nodes[kind] = {"added": added, "removed": removed, "changed": changed}

    has_drift = (
        any(any(data[label] for label in ("added", "removed", "changed")) for data in headline.values())
        or any(count_deltas[section][kind] for section in count_deltas for kind in count_deltas[section])
        or bool(extractor_changes)
        or bool(other_nodes)
    )

    return {
        "has_drift": has_drift,
        "summary": "drift detected" if has_drift else "no drift",
        "headline": headline,
        "other_nodes": other_nodes,
        "count_deltas": count_deltas,
        "extractor_hash_changes": extractor_changes,
    }


def write_drift_report(
    out: Path,
    ts: str,
    drift: dict[str, Any],
    previous_path: Path | None,
    current_path: Path,
) -> Path:
    path = out / "drift" / f"{ts}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Drift Report: {ts}",
        "",
        f"Previous snapshot: {previous_path.name if previous_path else '(none)'}",
        f"Current snapshot: {current_path.name}",
        f"Summary: {drift['summary']}",
        "",
        "## Headline Kinds",
    ]
    for kind in HEADLINE_KINDS:
        data = drift.get("headline", {}).get(kind, {"added": [], "removed": [], "changed": []})
        lines.append(f"### {kind}")
        for label in ("added", "removed", "changed"):
            values = data[label]
            lines.append(f"- {label}: {len(values)}" + (f" ({', '.join(values[:12])}{' ...' if len(values) > 12 else ''})" if values else ""))
    lines.extend(["", "## Count Deltas"])
    for section in ("nodes", "edges"):
        lines.append(f"### {section.title()}")
        deltas = drift.get("count_deltas", {}).get(section, {})
        if not deltas:
            lines.append("- no count changes")
        for kind, delta in sorted(deltas.items()):
            if delta:
                lines.append(f"- {kind}: {delta:+d}")
    lines.extend(["", "## Other Node Kinds"])
    other_nodes = drift.get("other_nodes", {})
    if not other_nodes:
        lines.append("- no non-headline node id changes")
    for kind, data in sorted(other_nodes.items()):
        lines.append(f"### {kind}")
        for label in ("added", "removed", "changed"):
            values = data[label]
            lines.append(f"- {label}: {len(values)}" + (f" ({', '.join(values[:12])}{' ...' if len(values) > 12 else ''})" if values else ""))
    lines.extend(["", "## Extractor Fact Hashes"])
    changes = drift.get("extractor_hash_changes", {})
    if not changes:
        lines.append("- no extractor fact hashes changed")
    for name, data in sorted(changes.items()):
        before = (data.get("before") or "missing")[:12]
        after = (data.get("after") or "missing")[:12]
        lines.append(f"- {name}: {before} -> {after}")
    path.write_text("\n".join(lines) + "\n")
    return path


def _load_target_or_die(name: str) -> TargetConfig:
    try:
        return load_target_by_name(name, TARGETS_DIR)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)


def _count_deltas(previous: dict[str, int], current: dict[str, int]) -> dict[str, int]:
    return {k: current.get(k, 0) - previous.get(k, 0) for k in sorted(set(previous) | set(current))}


def _read_json(path: Path | None) -> dict[str, Any] | None:
    return json.loads(path.read_text()) if path else None


def _digest_json(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _utc_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _parse_snapshot_ts(value: str) -> datetime | None:
    for fmt in ("%Y%m%dT%H%M%S%fZ", "%Y%m%dT%H%M%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
