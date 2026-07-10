"""Static test-to-code linkage extractor."""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from codeintel.config import Component, TargetConfig
from codeintel.extractors.base import Facts

log = logging.getLogger(__name__)

_PY_EXT = ".py"
_JS_EXTS = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
_MAX_FILE_BYTES = 2 * 1024 * 1024


@dataclass
class PyFileInfo:
    relpath: str
    module: str
    functions: dict[str, str] = field(default_factory=dict)


@dataclass
class ImportedBinding:
    relpath: str
    symbol: str | None = None


class TestsExtractor:
    name = "tests"

    def extract(self, target: TargetConfig) -> Facts:
        facts = Facts(extractor=self.name, target=target.name)
        py_files = _collect_python_files(target)
        module_index = {info.module: info.relpath for info in py_files.values()}
        for relpath in sorted(py_files):
            if _is_pytest_path(relpath):
                self._pytest_file(target, relpath, py_files, module_index, facts)
        for comp in sorted(target.components, key=lambda c: c.name):
            if comp.lang.lower() in {"javascript", "typescript"}:
                for relpath in _walk_component_files(target, comp, _JS_EXTS):
                    if _is_js_test_path(relpath):
                        facts.add_node(f"test:{relpath}", "Test", name=PurePosixPath(relpath).name, path=relpath)
        return facts

    def _pytest_file(
        self,
        target: TargetConfig,
        relpath: str,
        py_files: dict[str, PyFileInfo],
        module_index: dict[str, str],
        facts: Facts,
    ) -> None:
        path = target.root / relpath
        try:
            tree = ast.parse(path.read_text(errors="replace"), filename=relpath)
        except (OSError, SyntaxError) as e:
            log.warning("cannot parse test file %s: %s", relpath, e)
            return
        bindings = _import_bindings(tree, module_index)
        imported_files = sorted({b.relpath for b in bindings.values()})
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                test_id = f"test:{relpath}::{node.name}"
                facts.add_node(test_id, "Test", name=node.name, path=relpath, line=node.lineno)
                for imported_rel in imported_files:
                    facts.add_edge(f"file:{imported_rel}", test_id, "TESTED_BY", linkage="file")
                    for qname in sorted(py_files.get(imported_rel, PyFileInfo(imported_rel, "")).functions.values()):
                        facts.add_edge(f"fn:{imported_rel}::{qname}", test_id, "TESTED_BY", linkage="file")
                for fn_id in sorted(_called_imported_functions(node, bindings, py_files)):
                    facts.add_edge(fn_id, test_id, "TESTED_BY", linkage="function")


def _collect_python_files(target: TargetConfig) -> dict[str, PyFileInfo]:
    out: dict[str, PyFileInfo] = {}
    for comp in sorted(target.components, key=lambda c: c.name):
        if comp.lang.lower() != "python":
            continue
        for relpath in _walk_component_files(target, comp, {_PY_EXT}):
            path = target.root / relpath
            try:
                tree = ast.parse(path.read_text(errors="replace"), filename=relpath)
            except (OSError, SyntaxError) as e:
                log.warning("cannot parse python file %s: %s", relpath, e)
                continue
            module = _module_name(comp, relpath)
            info = PyFileInfo(relpath=relpath, module=module)
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    info.functions[node.name] = node.name
                elif isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            info.functions[f"{node.name}.{child.name}"] = f"{node.name}.{child.name}"
            out[relpath] = info
    return out


def _walk_component_files(target: TargetConfig, comp: Component, exts: set[str]) -> list[str]:
    excluded = set(target.exclude)
    root = target.component_root(comp)
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(part in excluded or part.startswith(".") for part in rel_parts):
            continue
        if not path.is_file() or path.suffix not in exts:
            continue
        try:
            if path.stat().st_size <= _MAX_FILE_BYTES:
                out.append(path.relative_to(target.root).as_posix())
        except OSError:
            continue
    return out


def _module_name(comp: Component, relpath: str) -> str:
    path = PurePosixPath(relpath)
    try:
        rel = path.relative_to(PurePosixPath(comp.path))
    except ValueError:
        rel = path
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _is_pytest_path(relpath: str) -> bool:
    name = PurePosixPath(relpath).name
    return relpath.endswith(".py") and ("tests/" in relpath or name.startswith("test_") or name.endswith("_test.py"))


def _is_js_test_path(relpath: str) -> bool:
    name = PurePosixPath(relpath).name
    return (
        ".spec." in name
        or ".test." in name
        or PurePosixPath(relpath).parent.name == "journeys" and name.endswith(".mjs")
    )


def _import_bindings(tree: ast.Module, module_index: dict[str, str]) -> dict[str, ImportedBinding]:
    bindings: dict[str, ImportedBinding] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if rel := _resolve_module(alias.name, module_index):
                    bindings[alias.asname or alias.name.split(".")[0]] = ImportedBinding(rel)
        elif isinstance(node, ast.ImportFrom):
            if not node.module:
                continue
            module = node.module
            if rel := _resolve_module(module, module_index):
                for alias in node.names:
                    if alias.name != "*":
                        bindings[alias.asname or alias.name] = ImportedBinding(rel, alias.name)
            for alias in node.names:
                submod = f"{module}.{alias.name}"
                if rel := _resolve_module(submod, module_index):
                    bindings[alias.asname or alias.name] = ImportedBinding(rel)
    return bindings


def _resolve_module(module: str, module_index: dict[str, str]) -> str | None:
    if module in module_index:
        return module_index[module]
    parts = module.split(".")
    for i in range(len(parts) - 1, 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in module_index:
            return module_index[prefix]
    return None


def _called_imported_functions(
    test_fn: ast.FunctionDef | ast.AsyncFunctionDef,
    bindings: dict[str, ImportedBinding],
    py_files: dict[str, PyFileInfo],
) -> set[str]:
    out: set[str] = set()
    for node in ast.walk(test_fn):
        if not isinstance(node, ast.Call):
            continue
        target = _call_binding(node.func, bindings)
        if target is None:
            continue
        rel, symbol = target
        info = py_files.get(rel)
        if info is None:
            continue
        if symbol in info.functions:
            out.add(f"fn:{rel}::{info.functions[symbol]}")
    return out


def _call_binding(func: ast.AST, bindings: dict[str, ImportedBinding]) -> tuple[str, str] | None:
    if isinstance(func, ast.Name):
        b = bindings.get(func.id)
        if b and b.symbol:
            return b.relpath, b.symbol
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        b = bindings.get(func.value.id)
        if b and b.symbol is None:
            return b.relpath, func.attr
        if b and b.symbol:
            return b.relpath, f"{b.symbol}.{func.attr}"
    return None
