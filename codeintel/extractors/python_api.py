"""FastAPI route extractor."""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path

from codeintel.config import Component, TargetConfig
from codeintel.extractors._http import (
    HTTP_METHODS,
    component_root,
    fn_id,
    join_url,
    repo_rel,
    route_id,
    should_skip,
)
from codeintel.extractors.base import Facts

log = logging.getLogger(__name__)


@dataclass
class ImportBinding:
    module: str
    name: str


@dataclass
class RouteDef:
    method: str
    path: str
    handler: str
    router: str
    line: int
    guards: set[str] = field(default_factory=set)


@dataclass
class FileApi:
    path: Path
    relpath: str
    module: str
    imports: dict[str, ImportBinding] = field(default_factory=dict)
    routers: dict[str, str] = field(default_factory=dict)
    routes: list[RouteDef] = field(default_factory=list)
    includes: list[tuple[str, str, list[str]]] = field(default_factory=list)
    functions: set[str] = field(default_factory=set)


class PythonApiExtractor:
    name = "python-api"

    def extract(self, target: TargetConfig) -> Facts:
        facts = Facts(extractor=self.name, target=target.name)
        for comp in target.components:
            if comp.framework != "fastapi":
                continue
            self._extract_component(target, comp, facts)
        return facts

    def _extract_component(self, target: TargetConfig, comp: Component, facts: Facts) -> None:
        root = component_root(target, comp)
        files: dict[str, FileApi] = {}
        for path in sorted(root.rglob("*.py")):
            if should_skip(path, target) or "__pycache__" in path.parts:
                continue
            info = self._parse_file(target, comp, path)
            if info is not None:
                files[info.module] = info

        router_prefixes: dict[tuple[str, str], list[str]] = {}
        for info in files.values():
            for alias, prefix, guards in info.includes:
                binding = info.imports.get(alias)
                if binding is None:
                    continue
                imported = files.get(binding.module)
                if imported is None:
                    continue
                router_prefixes.setdefault((binding.module, binding.name), []).append(prefix)
                # Router-level dependencies from include_router apply to all child routes.
                if guards:
                    for r in imported.routes:
                        r.guards.update(guards)

        for info in files.values():
            for route in info.routes:
                prefixes = [""]
                if route.router != "app":
                    prefixes = router_prefixes.get((info.module, route.router), [""])
                for prefix in sorted(set(prefixes)):
                    full_path = join_url(prefix, route.path)
                    rid = route_id(route.method, full_path)
                    handler_id = fn_id(info.relpath, route.handler)
                    facts.add_node(
                        rid,
                        "Route",
                        method=route.method,
                        path=full_path,
                        handler_path=info.relpath,
                        line=route.line,
                    )
                    facts.add_edge(f"component:{comp.name}", rid, "EXPOSES")
                    facts.add_edge(rid, handler_id, "HANDLED_BY")
                    for guard in sorted(route.guards):
                        gid = self._resolve_callable_id(info, files, guard)
                        if gid:
                            facts.add_edge(rid, gid, "GUARDED_BY")

    def _parse_file(self, target: TargetConfig, comp: Component, path: Path) -> FileApi | None:
        rel = repo_rel(target, path)
        module = _module_name(component_root(target, comp), path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as e:
            log.warning("python-api: skipping %s: %s", rel, e)
            return None
        info = FileApi(path=path, relpath=rel, module=module)
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                mod = _resolve_import(module, node.module or "", node.level)
                for alias in node.names:
                    info.imports[alias.asname or alias.name] = ImportBinding(mod, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    info.imports[alias.asname or alias.name.split(".")[0]] = ImportBinding(alias.name, alias.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                info.functions.add(node.name)
                for dec in node.decorator_list:
                    route = self._route_from_decorator(dec, node.name, node.lineno)
                    if route is not None:
                        route.guards.update(_depends_from_args(node.args.args + node.args.kwonlyargs))
                        info.routes.append(route)
                        if isinstance(dec, ast.Call):
                            route.guards.update(_depends_from_keywords(dec.keywords))
                guard_calls = _guard_calls(node)
                if guard_calls:
                    for route in info.routes:
                        if route.handler == node.name:
                            route.guards.update(guard_calls)
            elif isinstance(node, ast.Assign):
                for target_node in node.targets:
                    if isinstance(target_node, ast.Name) and _is_call_named(node.value, "APIRouter"):
                        info.routers[target_node.id] = _literal_kw(node.value, "prefix") or ""
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                call = node.value
                if _attr_name(call.func) == "include_router" and call.args:
                    alias = _name(call.args[0])
                    if alias:
                        prefix = _literal_kw(call, "prefix") or ""
                        info.includes.append((alias, prefix, _depends_from_keywords(call.keywords)))
        return info

    def _route_from_decorator(self, node: ast.AST, handler: str, line: int) -> RouteDef | None:
        if not isinstance(node, ast.Call):
            return None
        func = node.func
        if not isinstance(func, ast.Attribute):
            return None
        method = func.attr.upper()
        if method not in HTTP_METHODS:
            return None
        receiver = _name(func.value)
        if not receiver:
            return None
        path = _literal_arg(node, 0) or ""
        return RouteDef(method=method, path=path, handler=handler, router=receiver, line=line)

    def _resolve_callable_id(self, info: FileApi, files: dict[str, FileApi], name: str) -> str | None:
        local = name.split(".")[-1]
        if local in info.functions:
            return fn_id(info.relpath, local)
        binding = info.imports.get(local)
        if binding is None:
            return None
        imported = files.get(binding.module)
        if imported and binding.name in imported.functions:
            return fn_id(imported.relpath, binding.name)
        return None


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    parts = [p for p in rel.parts if p != "__init__"]
    return ".".join(parts)


def _resolve_import(current: str, module: str, level: int) -> str:
    if level <= 0:
        return module
    base = current.split(".")[:-level]
    if module:
        base.extend(module.split("."))
    return ".".join(p for p in base if p)


def _name(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _attr_name(node: ast.AST | None) -> str | None:
    return node.attr if isinstance(node, ast.Attribute) else None


def _literal_arg(call: ast.Call, idx: int) -> str | None:
    if len(call.args) <= idx:
        return None
    return call.args[idx].value if isinstance(call.args[idx], ast.Constant) and isinstance(call.args[idx].value, str) else None


def _literal_kw(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _is_call_named(node: ast.AST, name: str) -> bool:
    return isinstance(node, ast.Call) and (_name(node.func) or "").endswith(name)


def _depends_from_args(args: list[ast.arg]) -> set[str]:
    out: set[str] = set()
    for arg in args:
        if arg.annotation is not None:
            out.update(_depends_names(arg.annotation))
    return out


def _depends_from_keywords(keywords: list[ast.keyword]) -> set[str]:
    out: set[str] = set()
    for kw in keywords:
        if kw.arg == "dependencies":
            out.update(_depends_names(kw.value))
    return out


def _depends_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and (_name(child.func) or "").endswith("Depends") and child.args:
            n = _name(child.args[0])
            if n:
                out.add(n)
    return out


def _guard_calls(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    out: set[str] = set()
    for child in ast.walk(fn):
        if isinstance(child, ast.Call):
            n = _name(child.func)
            if n and (n.startswith("ensure_") or ".ensure_" in n):
                out.add(n)
    return out
