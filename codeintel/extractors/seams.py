"""Frontend/backend seam extractor."""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from codeintel.config import Component, TargetConfig
from codeintel.extractors._http import (
    HTTP_METHODS,
    app_route_url,
    comparable_path,
    component_root,
    fn_id,
    join_url,
    next_api_to_backend,
    page_id,
    proxy_id,
    repo_rel,
    route_id,
    should_skip,
)
from codeintel.extractors.base import Facts

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BackendRoute:
    method: str
    path: str
    id: str


@dataclass
class FetchCall:
    src: str
    url: str
    method: str | None = None


class SeamsExtractor:
    name = "seams"

    def extract(self, target: TargetConfig) -> Facts:
        facts = Facts(extractor=self.name, target=target.name)
        routes = self._backend_routes(target)
        for comp in target.components:
            if comp.framework != "nextjs":
                continue
            self._extract_frontend(target, comp, routes, facts)
        return facts

    def _backend_routes(self, target: TargetConfig) -> list[BackendRoute]:
        out: list[BackendRoute] = []
        for comp in target.components:
            if comp.framework != "fastapi":
                continue
            root = component_root(target, comp)
            prefixes = self._include_prefixes(root)
            for path in sorted(root.rglob("*.py")):
                if should_skip(path, target) or "__pycache__" in path.parts:
                    continue
                try:
                    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                except (OSError, SyntaxError, UnicodeDecodeError) as e:
                    log.warning("seams: skipping %s: %s", repo_rel(target, path), e)
                    continue
                module = _module_name(root, path)
                for node in tree.body:
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for dec in node.decorator_list:
                        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                            continue
                        method = dec.func.attr.upper()
                        if method not in HTTP_METHODS:
                            continue
                        route_path = _literal_arg(dec, 0) or ""
                        parent_prefixes = prefixes.get(module, [""])
                        for prefix in parent_prefixes:
                            full = join_url(prefix, route_path)
                            out.append(BackendRoute(method, full, route_id(method, full)))
        return sorted(set(out), key=lambda r: (r.method, r.path, r.id))

    def _include_prefixes(self, root: Path) -> dict[str, list[str]]:
        prefixes: dict[str, list[str]] = {}
        for path in sorted(root.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            module = _module_name(root, path)
            imports: dict[str, str] = {}
            for node in tree.body:
                if isinstance(node, ast.ImportFrom):
                    mod = _resolve_import(module, node.module or "", node.level)
                    for alias in node.names:
                        imports[alias.asname or alias.name] = mod
                elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    call = node.value
                    if isinstance(call.func, ast.Attribute) and call.func.attr == "include_router" and call.args:
                        alias = _name(call.args[0])
                        mod = imports.get(alias or "")
                        if mod:
                            prefixes.setdefault(mod, []).append(_literal_kw(call, "prefix") or "")
        return {k: sorted(set(v)) for k, v in prefixes.items()}

    def _extract_frontend(
        self,
        target: TargetConfig,
        comp: Component,
        routes: list[BackendRoute],
        facts: Facts,
    ) -> None:
        root = component_root(target, comp)
        app_root = root / "app"
        has_rewrite = self._has_api_rewrite(root)
        for path in sorted(root.rglob("*")):
            if should_skip(path, target) or path.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
                continue
            if any(part in {".next", "node_modules"} for part in path.parts):
                continue
            for call in self._fetches_in_file(target, root, app_root, path):
                dst = proxy_id(call.url) if has_rewrite and call.url.startswith("/api/") else self._best_route_id(call, routes)
                if call.src.startswith("fn:"):
                    src_path, _, qname = call.src.removeprefix("fn:").partition("::")
                    facts.add_node(call.src, "Function", path=src_path, qname=qname, name=qname.split(".")[-1])
                facts.add_node(dst, "ApiProxy" if dst.startswith("proxy:") else "Route", route=call.url, path=repo_rel(target, path))
                facts.add_edge(call.src, dst, "FETCHES", method=call.method)
                if dst.startswith("proxy:"):
                    backend = next_api_to_backend(call.url)
                    if backend:
                        route = self._match_route(backend, call.method, routes)
                        if route:
                            facts.add_node(dst, "ApiProxy", route=call.url, backend_path=backend, path=repo_rel(target, path))
                            facts.add_edge(dst, route.id, "PROXIES")

    def _fetches_in_file(self, target: TargetConfig, root: Path, app_root: Path, path: Path) -> list[FetchCall]:
        rel = repo_rel(target, path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log.warning("seams: skipping %s: %s", rel, e)
            return []
        consts = _string_consts(text)
        line_starts = _line_starts(text)
        default_src = fn_id(rel, PureName(path.stem).qname)
        if app_root in path.parents and re.match(r"page\.(tsx|ts|jsx|js)$", path.name):
            default_src = page_id(app_route_url(app_root, path))
        calls: list[FetchCall] = []
        for m in _CALL_RE.finditer(text):
            url = _resolve_url_expr(m.group("url"), consts)
            if not url:
                continue
            method = _method_near(text[m.end():m.end() + 260])
            src = _enclosing_source(text, line_starts, m.start(), rel, default_src)
            calls.append(FetchCall(src=src, url=url, method=method))
        return calls

    def _has_api_rewrite(self, root: Path) -> bool:
        for name in ("next.config.ts", "next.config.js"):
            p = root / name
            if p.exists():
                try:
                    text = p.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                return "/api/:path*" in text and "/api/v1/:path*" in text
        return False

    def _best_route_id(self, call: FetchCall, routes: list[BackendRoute]) -> str:
        route = self._match_route(call.url, call.method, routes)
        if route:
            return route.id
        return route_id(call.method or "GET", call.url)

    def _match_route(self, path: str, method: str | None, routes: list[BackendRoute]) -> BackendRoute | None:
        cpath = comparable_path(path)
        method = method.upper() if method else None
        matches = [r for r in routes if comparable_path(r.path) == cpath]
        if method:
            exact = [r for r in matches if r.method == method]
            if exact:
                return exact[0]
        return matches[0] if matches else None


@dataclass(frozen=True)
class PureName:
    raw: str

    @property
    def qname(self) -> str:
        return re.sub(r"\W+", "_", self.raw).strip("_") or "module"


_CALL_RE = re.compile(
    r"\b(?:fetch|request|axios(?:\.(?:get|post|put|patch|delete))?)\s*\(\s*(?P<url>`[^`]+`|'[^']+'|\"[^\"]+\"|[A-Za-z_$][\w$]*(?:\s*\+\s*(?:`[^`]+`|'[^']+'|\"[^\"]+\"))?)",
    re.MULTILINE,
)


def _string_consts(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(`[^`]+`|'[^']+'|\"[^\"]+\")", text):
        url = _clean_url(m.group(2), out)
        if url:
            out[m.group(1)] = url
    return out


def _resolve_url_expr(expr: str, consts: dict[str, str]) -> str | None:
    return _clean_url(expr, consts)


def _clean_url(expr: str, consts: dict[str, str]) -> str | None:
    expr = expr.strip()
    if expr in consts:
        return consts[expr]
    parts = [p.strip() for p in expr.split("+")]
    built = ""
    for p in parts:
        if p in consts:
            built += consts[p]
        elif (p.startswith(("'", '"', "`")) and p.endswith(("'", '"', "`"))):
            built += p[1:-1]
        elif built:
            built += "[id]"
        else:
            return None
    if not built:
        return None
    if built.startswith(("http://", "https://", "//")):
        if "localhost" not in built and "127.0.0.1" not in built:
            return None
        try:
            built = "/" + built.split("/", 3)[3]
        except IndexError:
            return None
    built = re.sub(r"\$\{[^}]*\bq\b[^}]*\}$", "", built)
    built = re.sub(r"\$\{[^}]+\}", "[id]", built)
    built = built.split("?", 1)[0].split("#", 1)[0]
    if not built.startswith("/"):
        return None
    return join_url(built)


def _method_near(text: str) -> str | None:
    m = re.search(r"\bmethod\s*:\s*['\"]([A-Za-z]+)['\"]", text)
    return m.group(1).upper() if m else None


def _line_starts(text: str) -> list[int]:
    return [0] + [m.end() for m in re.finditer(r"\n", text)]


def _line_for(starts: list[int], pos: int) -> int:
    lo = 0
    for i, start in enumerate(starts):
        if start > pos:
            break
        lo = i
    return lo + 1


def _enclosing_source(text: str, starts: list[int], pos: int, rel: str, default_src: str) -> str:
    prefix = text[:pos]
    best: tuple[int, str] | None = None
    for pat in (
        r"(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>",
    ):
        for m in re.finditer(pat, prefix):
            if best is None or m.start() > best[0]:
                best = (m.start(), m.group(1))
    if best is not None:
        return fn_id(rel, best[1])
    _ = _line_for(starts, pos)
    return default_src


def _module_name(root: Path, path: Path) -> str:
    rel = path.relative_to(root).with_suffix("")
    return ".".join(p for p in rel.parts if p != "__init__")


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
        return node.attr
    return None


def _literal_arg(call: ast.Call, idx: int) -> str | None:
    if len(call.args) <= idx:
        return None
    arg = call.args[idx]
    return arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None


def _literal_kw(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None
