"""Dependency extractor for Python and npm manifests/lockfiles."""

from __future__ import annotations

import json
import logging
import re
import tomllib
from pathlib import Path
from typing import Any

from codeintel.config import Component, TargetConfig
from codeintel.extractors.base import Facts

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^\s*([A-Za-z0-9_.-]+)")


class DependenciesExtractor:
    name = "dependencies"

    def extract(self, target: TargetConfig) -> Facts:
        facts = Facts(extractor=self.name, target=target.name)
        for comp in sorted(target.components, key=lambda c: c.name):
            root = target.component_root(comp)
            if (root / "pyproject.toml").exists() or (root / "uv.lock").exists():
                self._extract_python(target, comp, facts)
            else:
                log.warning("component %s: no pyproject.toml/uv.lock", comp.name)
            if (root / "package.json").exists() or (root / "package-lock.json").exists():
                self._extract_npm(target, comp, facts)
            else:
                log.warning("component %s: no package.json/package-lock.json", comp.name)
        return facts

    def _extract_python(self, target: TargetConfig, comp: Component, facts: Facts) -> None:
        root = target.component_root(comp)
        manifest = _read_toml(root / "pyproject.toml")
        lock = _read_toml(root / "uv.lock")
        declared = _py_declared(manifest)
        git_sources = _py_git_sources(manifest)
        locked = _uv_locked(lock)
        component_id = f"component:{comp.name}"

        for name in sorted(set(locked) | set(declared)):
            source, dep_id = _py_dep_identity(name, git_sources, locked.get(name, {}))
            props: dict[str, Any] = {"name": name, "source": source}
            if version := locked.get(name, {}).get("version"):
                props["locked_version"] = version
            if name not in declared:
                props["transitive"] = True
            facts.add_node(dep_id, "Dependency", **props)
            if name in declared:
                facts.add_edge(
                    component_id,
                    dep_id,
                    "DEPENDS_ON",
                    declared_version=declared[name],
                    locked_version=props.get("locked_version"),
                    source=source,
                )

    def _extract_npm(self, target: TargetConfig, comp: Component, facts: Facts) -> None:
        root = target.component_root(comp)
        manifest = _read_json(root / "package.json")
        lock = _read_json(root / "package-lock.json")
        declared = _npm_declared(manifest)
        locked = _npm_locked(lock)
        component_id = f"component:{comp.name}"

        for name in sorted(set(locked) | set(declared)):
            dep_id = f"dep:npm/{name}"
            props: dict[str, Any] = {"name": name, "source": "npm"}
            if version := locked.get(name):
                props["locked_version"] = version
            if name not in declared:
                props["transitive"] = True
            facts.add_node(dep_id, "Dependency", **props)
            if name in declared:
                facts.add_edge(
                    component_id,
                    dep_id,
                    "DEPENDS_ON",
                    declared_version=declared[name],
                    locked_version=props.get("locked_version"),
                    source="npm",
                )


def _read_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        log.warning("missing %s", path)
        return {}
    try:
        return tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        log.warning("cannot parse %s: %s", path, e)
        return {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        log.warning("missing %s", path)
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as e:
        log.warning("cannot parse %s: %s", path, e)
        return {}


def _canonical_py_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _dep_name(spec: str) -> str | None:
    match = _NAME_RE.match(spec)
    return _canonical_py_name(match.group(1)) if match else None


def _py_declared(doc: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for spec in doc.get("project", {}).get("dependencies", []) or []:
        if isinstance(spec, str) and (name := _dep_name(spec)):
            out[name] = spec
    return out


def _py_git_sources(doc: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    sources = doc.get("tool", {}).get("uv", {}).get("sources", {}) or {}
    if isinstance(sources, dict):
        for name, spec in sources.items():
            if isinstance(spec, dict) and isinstance(spec.get("git"), str):
                out[_canonical_py_name(str(name))] = spec["git"]
    return out


def _uv_locked(doc: dict[str, Any]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for pkg in doc.get("package", []) or []:
        if not isinstance(pkg, dict) or not isinstance(pkg.get("name"), str):
            continue
        name = _canonical_py_name(pkg["name"])
        item: dict[str, str] = {}
        if isinstance(pkg.get("version"), str):
            item["version"] = pkg["version"]
        source = pkg.get("source")
        if isinstance(source, dict) and isinstance(source.get("git"), str):
            item["git"] = source["git"]
        out[name] = item
    return out


def _py_dep_identity(name: str, git_sources: dict[str, str], lock: dict[str, str]) -> tuple[str, str]:
    git = lock.get("git") or git_sources.get(name)
    if git:
        return f"git+{git}", f"dep:git/{name}"
    return "pypi", f"dep:pypi/{name}"


def _npm_declared(doc: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        deps = doc.get(section, {}) or {}
        if isinstance(deps, dict):
            for name, spec in deps.items():
                if isinstance(name, str):
                    out[name] = str(spec)
    return out


def _npm_locked(doc: dict[str, Any]) -> dict[str, str]:
    packages = doc.get("packages", {}) or {}
    out: dict[str, str] = {}
    if isinstance(packages, dict):
        for path, meta in packages.items():
            if not isinstance(path, str) or not path.startswith("node_modules/"):
                continue
            if not isinstance(meta, dict) or not isinstance(meta.get("version"), str):
                continue
            name = path.removeprefix("node_modules/")
            out[name] = meta["version"]
    return out
