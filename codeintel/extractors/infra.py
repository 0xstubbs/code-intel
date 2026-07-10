"""Infrastructure/static environment extractor."""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path
from urllib.parse import urlparse

from codeintel.config import Component, TargetConfig
from codeintel.extractors.base import Facts

log = logging.getLogger(__name__)

_PY_EXT = ".py"
_JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_MAX_FILE_BYTES = 2 * 1024 * 1024
_PROCESS_ENV_RE = re.compile(r"\bprocess\.env\.([A-Za-z_][A-Za-z0-9_]*)")
_BRACKET_ENV_RE = re.compile(r"\bprocess\.env\[['\"]([A-Za-z_][A-Za-z0-9_]*)['\"]\]")
_DOCKER_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", re.IGNORECASE)

_IMPORT_SERVICES = {
    "google.cloud.storage": ("svc:gcs", "Google Cloud Storage"),
    "psycopg": ("svc:postgres", "Postgres"),
    "psycopg2": ("svc:postgres", "Postgres"),
    "asyncpg": ("svc:postgres", "Postgres"),
    "langfuse": ("svc:langfuse", "Langfuse"),
}


class InfraExtractor:
    name = "infra"

    def extract(self, target: TargetConfig) -> Facts:
        facts = Facts(extractor=self.name, target=target.name)
        for comp in sorted(target.components, key=lambda c: c.name):
            self._docker(target, comp, facts)
            for relpath in _walk_source_files(target, comp):
                path = target.root / relpath
                if path.suffix == _PY_EXT:
                    self._python_file(path, relpath, facts)
                elif path.suffix in _JS_EXTS:
                    self._js_file(path, relpath, facts)
        return facts

    def _docker(self, target: TargetConfig, comp: Component, facts: Facts) -> None:
        roots = [target.component_root(comp), target.root]
        seen: set[Path] = set()
        for root in roots:
            try:
                candidates = sorted(p for p in root.iterdir() if p.name.startswith("Dockerfile"))
            except OSError as e:
                log.warning("cannot list dockerfiles in %s: %s", root, e)
                continue
            for path in candidates:
                if path in seen or not path.is_file():
                    continue
                seen.add(path)
                try:
                    rel = path.relative_to(target.root).as_posix()
                    for line in path.read_text(errors="replace").splitlines():
                        match = _DOCKER_FROM_RE.match(line)
                        if not match:
                            continue
                        image = match.group(1)
                        facts.add_node(
                            f"svc:docker-base/{image}",
                            "Service",
                            name=image,
                            service_type="docker-base",
                            image=image,
                            component=comp.name,
                            path=rel,
                        )
                except OSError as e:
                    log.warning("cannot read %s: %s", path, e)
        if not seen:
            log.warning("component %s: no Dockerfile found", comp.name)

    def _python_file(self, path: Path, relpath: str, facts: Facts) -> None:
        try:
            tree = ast.parse(path.read_text(errors="replace"), filename=relpath)
        except (OSError, SyntaxError) as e:
            log.warning("cannot parse %s: %s", relpath, e)
            return
        visitor = _PyInfraVisitor()
        visitor.visit(tree)
        fid = f"file:{relpath}"
        for env in sorted(visitor.env_vars):
            facts.add_node(f"env:{env}", "EnvVar", name=env)
            facts.add_edge(fid, f"env:{env}", "READS_ENV")
        for sid, name in sorted(visitor.services.values()):
            facts.add_node(sid, "Service", name=name)
            facts.add_edge(fid, sid, "USES_SERVICE")
        for url in sorted(visitor.urls):
            if sid_name := _service_from_url(url):
                sid, name = sid_name
                facts.add_node(sid, "Service", name=name, url=url)
                facts.add_edge(fid, sid, "USES_SERVICE", url=url)

    def _js_file(self, path: Path, relpath: str, facts: Facts) -> None:
        try:
            text = path.read_text(errors="replace")
        except OSError as e:
            log.warning("cannot read %s: %s", relpath, e)
            return
        fid = f"file:{relpath}"
        envs = set(_PROCESS_ENV_RE.findall(text)) | set(_BRACKET_ENV_RE.findall(text))
        for env in sorted(envs):
            facts.add_node(f"env:{env}", "EnvVar", name=env)
            facts.add_edge(fid, f"env:{env}", "READS_ENV")
        for spec, sid_name in _IMPORT_SERVICES.items():
            if re.search(rf"from\s+['\"]{re.escape(spec)}['\"]|require\(['\"]{re.escape(spec)}['\"]\)", text):
                sid, name = sid_name
                facts.add_node(sid, "Service", name=name)
                facts.add_edge(fid, sid, "USES_SERVICE")
        for url in sorted(set(re.findall(r"['\"](https?://[^'\"\s)]+)['\"]", text))):
            if sid_name := _service_from_url(url):
                sid, name = sid_name
                facts.add_node(sid, "Service", name=name, url=url)
                facts.add_edge(fid, sid, "USES_SERVICE", url=url)


class _PyInfraVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.env_vars: set[str] = set()
        self.services: dict[str, tuple[str, str]] = {}
        self.urls: set[str] = set()
        self._settings_depth = 0

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._service_for_import(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        self._service_for_import(mod)
        for alias in node.names:
            self._service_for_import(f"{mod}.{alias.name}" if mod else alias.name)
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        was_settings = self._is_settings_class(node)
        if was_settings:
            self._settings_depth += 1
        self.generic_visit(node)
        if was_settings:
            self._settings_depth -= 1

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if self._settings_depth and isinstance(node.target, ast.Name):
            self.env_vars.add(node.target.id.upper())
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._settings_depth:
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.env_vars.add(target.id.upper())
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _is_attr(node.func, "os", "getenv") and node.args:
            if env := _literal_str(node.args[0]):
                self.env_vars.add(env)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "post", "put", "patch", "delete", "request"}:
            if _call_receiver_name(node.func) in {"requests", "httpx"} and node.args:
                if url := _literal_str(node.args[0]):
                    self.urls.add(url)
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _is_attr(node.value, "os", "environ"):
            if env := _literal_str(node.slice):
                self.env_vars.add(env)
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and node.value.startswith(("http://", "https://")):
            self.urls.add(node.value)

    def _service_for_import(self, module: str) -> None:
        for prefix, service in _IMPORT_SERVICES.items():
            if module == prefix or module.startswith(prefix + "."):
                self.services[service[0]] = service

    @staticmethod
    def _is_settings_class(node: ast.ClassDef) -> bool:
        for base in node.bases:
            name = _dotted_name(base)
            if name and name.split(".")[-1] in {"BaseSettings", "SettingsConfigDict"}:
                return True
        return False


def _walk_source_files(target: TargetConfig, comp: Component) -> list[str]:
    excluded = set(target.exclude)
    root = target.component_root(comp)
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in excluded or part.startswith(".") for part in path.relative_to(root).parts):
            continue
        if not path.is_file() or path.suffix not in {_PY_EXT, *_JS_EXTS}:
            continue
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            out.append(path.relative_to(target.root).as_posix())
        except OSError:
            continue
    return out


def _literal_str(node: ast.AST) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_attr(node: ast.AST, obj: str, attr: str) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == attr and _dotted_name(node.value) == obj


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return None


def _call_receiver_name(node: ast.Attribute) -> str | None:
    return _dotted_name(node.value)


def _service_from_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    sid = "svc:http/" + re.sub(r"[^a-z0-9_.:-]+", "-", host)
    return sid, host
