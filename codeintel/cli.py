"""codeintel CLI (argparse — deps kept lean per CONTRACTS.md).

Subcommands: index, refresh (stub), query, map (stub), targets, mcp (stub).
"""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
import time
from pathlib import Path

from codeintel import OUT_DIR, TARGETS_DIR
from codeintel.config import ConfigError, TargetConfig, list_targets, load_target_by_name
from codeintel.core.graph import KuzuGraphStore
from codeintel.extractors import REGISTRY, write_facts
from codeintel.queries import CANNED, QueryError

log = logging.getLogger("codeintel")


def _target_out(target: str) -> Path:
    return OUT_DIR / target


def _load_target_or_die(name: str) -> TargetConfig:
    try:
        return load_target_by_name(name, TARGETS_DIR)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(1)


def cmd_index(args: argparse.Namespace) -> int:
    target = _load_target_or_die(args.target)
    out = _target_out(target.name)
    facts_dir = out / "facts"
    facts_dir.mkdir(parents=True, exist_ok=True)

    failures = 0
    for name in target.extractors:
        extractor = REGISTRY.get(name)
        if extractor is None:
            log.warning("extractor '%s' configured but not yet in REGISTRY; skipping", name)
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
        return 1

    store = KuzuGraphStore(out / "graph.kuzu")
    load = store.load_facts(fact_files)
    log.info("graph loaded: %d nodes, %d edges from %d fact files (%d dangling edges dropped)",
             load.nodes, load.edges, load.fact_files, load.dangling_edges_dropped)
    print(json.dumps(store.stats(), indent=2, sort_keys=True))
    store.close()
    return 1 if failures and load.nodes == 0 else 0


def cmd_query(args: argparse.Namespace) -> int:
    target = _load_target_or_die(args.target)
    db = _target_out(target.name) / "graph.kuzu"
    if not db.exists():
        print(f"error: no graph at {db}; run `codeintel index {target.name}` first",
              file=sys.stderr)
        return 1
    if args.cypher:
        cypher = args.cypher
    else:
        if not args.name:
            print("error: provide a canned query name or --cypher", file=sys.stderr)
            print(f"canned queries: {', '.join(sorted(CANNED))}", file=sys.stderr)
            return 1
        fn = CANNED.get(args.name)
        if fn is None:
            print(f"error: unknown canned query '{args.name}'. "
                  f"Available: {', '.join(sorted(CANNED))}", file=sys.stderr)
            return 1
        try:
            cypher = fn(args.arg)
        except QueryError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    store = KuzuGraphStore(db)
    try:
        rows = store.query(cypher)
    except RuntimeError as e:
        print(f"query error: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(json.dumps(rows, indent=2, sort_keys=True))
    return 0


def cmd_targets(args: argparse.Namespace) -> int:
    paths = list_targets(TARGETS_DIR)
    if not paths:
        print(f"no targets configured under {TARGETS_DIR}")
        return 0
    for p in paths:
        try:
            t = load_target_by_name(p.stem, TARGETS_DIR)
            comps = ", ".join(c.name for c in t.components)
            print(f"{t.name}  root={t.root}  components=[{comps}]  "
                  f"extractors=[{', '.join(t.extractors)}]")
        except ConfigError as e:
            print(f"{p.stem}  INVALID: {e}")
    return 0


def cmd_stub(args: argparse.Namespace) -> int:
    """Dispatch to a feature module if it has landed; stub otherwise.

    Workers add codeintel/mapgen.py, codeintel/monitor.py, codeintel/mcp_server.py
    (each exposing `cli_run(args) -> int`) without editing this file.
    """
    module = {"map": "codeintel.mapgen", "refresh": "codeintel.monitor",
              "mcp": "codeintel.mcp_server"}.get(args.command)
    if module:
        try:
            mod = importlib.import_module(module)
        except ImportError:
            mod = None
        if mod is not None:
            return mod.cli_run(args)
    print(f"codeintel {args.command}: not yet implemented", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="codeintel",
                                description="Project-agnostic code-intelligence platform")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("index", help="run extractors, write facts, load graph")
    s.add_argument("target")
    s.set_defaults(fn=cmd_index)

    s = sub.add_parser("refresh", help="re-extract + drift report")
    s.add_argument("target")
    s.add_argument("--check", action="store_true",
                   help="exit 1 if drift detected (CI mode)")
    s.set_defaults(fn=cmd_stub)

    s = sub.add_parser("query", help="canned or raw Cypher query against the graph")
    s.add_argument("target")
    s.add_argument("name", nargs="?", help=f"canned query: {', '.join(sorted(CANNED))}")
    s.add_argument("arg", nargs="?", help="argument for the canned query")
    s.add_argument("--cypher", help="raw Cypher query (overrides canned name)")
    s.set_defaults(fn=cmd_query)

    s = sub.add_parser("map", help="generate architecture map + viz (not yet implemented)")
    s.add_argument("target")
    s.set_defaults(fn=cmd_stub)

    s = sub.add_parser("targets", help="list configured targets")
    s.set_defaults(fn=cmd_targets)

    s = sub.add_parser("mcp", help="serve query surface over MCP (not yet implemented)")
    s.add_argument("target")
    s.set_defaults(fn=cmd_stub)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
