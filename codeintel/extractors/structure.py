"""Structural extractor: files, classes, functions, imports, calls, inheritance.

Implementation choice (W1 bake-off): direct tree-sitter extraction.
CodeGraphContext 0.5.1 was evaluated first (it has a native Kuzu backend,
selectable via CGC_RUNTIME_DB_TYPE=kuzudb + KUZUDB_PATH) but (a) crashes on
Beancounter's backend in `_resolve_decorator_path` (imported decorator names
mapping to None) — fixable with a monkeypatch — and (b) took ~263s to index
ui/backend alone in-process. Owning ~600 lines of tree-sitter walking is
cheaper than owning a patched pin of a slow release. This module is *just
another producer of the fact format*; an adopted core can replace it later
without touching the loader or queries.

Emits (CONTRACTS.md v1):
  nodes: Target, Component, File, Module (unresolved imports), Class, Function
  edges: CONTAINS, DEFINES, IMPORTS, CALLS (best-effort same-file/imported-name),
         INHERITS

Best-effort CALLS covers: same-file functions, `self.`/`this.` methods,
`from x import f; f()` / named TS imports, `import m; m.f()` /
namespace imports, `Class.method()` on local/imported classes, and JSX
component usage (<Foo/> => CALLS to the component function).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from tree_sitter import Language, Node, Parser

from codeintel.config import Component, TargetConfig
from codeintel.extractors.base import Facts

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 2 * 1024 * 1024

LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
}

_TS_EXT_CANDIDATES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def _languages() -> dict[str, Language]:
    import tree_sitter_javascript
    import tree_sitter_python
    import tree_sitter_typescript

    return {
        "python": Language(tree_sitter_python.language()),
        "typescript": Language(tree_sitter_typescript.language_typescript()),
        "tsx": Language(tree_sitter_typescript.language_tsx()),
        "javascript": Language(tree_sitter_javascript.language()),
    }


# ── per-file intermediate model ─────────────────────────────────────────────


@dataclass
class FnDef:
    name: str
    qname: str
    line: int
    class_name: str | None = None  # enclosing class, if a method


@dataclass
class ClassDef:
    name: str
    qname: str
    line: int
    bases: list[str] = field(default_factory=list)


@dataclass
class Imp:
    """One import binding/module reference (language-neutral)."""
    module: str            # python dotted module or TS import spec
    level: int = 0         # python relative-import level
    symbol: str | None = None   # from-import / named-import symbol ("*" = wildcard)
    alias: str | None = None    # local binding name
    line: int = 0


@dataclass
class Call:
    caller_qname: str      # enclosing function qname
    kind: str              # "name" | "self" | "mod"
    obj: str | None        # receiver identifier for kind == "mod"
    callee: str
    line: int


@dataclass
class FileInfo:
    relpath: str
    component: str
    lang: str
    functions: list[FnDef] = field(default_factory=list)
    classes: list[ClassDef] = field(default_factory=list)
    imports: list[Imp] = field(default_factory=list)
    calls: list[Call] = field(default_factory=list)

    def top_level_fn(self, name: str) -> FnDef | None:
        for f in self.functions:
            if f.name == name and f.class_name is None and f.qname == name:
                return f
        return None

    def any_fn(self, name: str) -> FnDef | None:
        for f in self.functions:
            if f.name == name and f.class_name is None:
                return f
        return None

    def method(self, class_name: str, name: str) -> FnDef | None:
        for f in self.functions:
            if f.class_name == class_name and f.name == name:
                return f
        return None

    def cls(self, name: str) -> ClassDef | None:
        for c in self.classes:
            if c.name == name:
                return c
        return None


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _line(node: Node) -> int:
    return node.start_point[0] + 1


# ── python AST walk ─────────────────────────────────────────────────────────


def _walk_python(node: Node, info: FileInfo, class_stack: list[str], fn_stack: list[str]) -> None:
    for child in node.children:
        t = child.type
        if t == "class_definition":
            name = _text(child.child_by_field_name("name"))
            qname = ".".join([*class_stack, name])
            bases: list[str] = []
            sup = child.child_by_field_name("superclasses")
            if sup is not None:
                for b in sup.named_children:
                    if b.type in ("identifier", "attribute"):
                        bases.append(_text(b).split(".")[-1])
            info.classes.append(ClassDef(name, qname, _line(child), bases))
            body = child.child_by_field_name("body")
            if body is not None:
                _walk_python(body, info, [*class_stack, name], fn_stack)
        elif t == "function_definition":
            name = _text(child.child_by_field_name("name"))
            enclosing_class = class_stack[-1] if class_stack else None
            qname = ".".join([*class_stack, *fn_stack, name])
            info.functions.append(FnDef(name, qname, _line(child), enclosing_class))
            body = child.child_by_field_name("body")
            if body is not None:
                _walk_python(body, info, class_stack, [*fn_stack, name])
        elif t == "import_statement":
            for c in child.named_children:
                if c.type == "dotted_name":
                    mod = _text(c)
                    info.imports.append(Imp(mod, 0, None, mod.split(".")[0], _line(c)))
                elif c.type == "aliased_import":
                    mod = _text(c.child_by_field_name("name"))
                    alias = _text(c.child_by_field_name("alias"))
                    info.imports.append(Imp(mod, 0, None, alias, _line(c)))
        elif t == "import_from_statement":
            mod_node = child.child_by_field_name("module_name")
            module, level = "", 0
            if mod_node is not None:
                if mod_node.type == "relative_import":
                    raw = _text(mod_node)
                    level = len(raw) - len(raw.lstrip("."))
                    module = raw.lstrip(".")
                else:
                    module = _text(mod_node)
            for c in child.named_children:
                if mod_node is not None and c.id == mod_node.id:
                    continue
                if c.type == "dotted_name":
                    sym = _text(c)
                    info.imports.append(Imp(module, level, sym, sym.split(".")[-1], _line(c)))
                elif c.type == "aliased_import":
                    sym = _text(c.child_by_field_name("name"))
                    alias = _text(c.child_by_field_name("alias"))
                    info.imports.append(Imp(module, level, sym, alias, _line(c)))
                elif c.type == "wildcard_import":
                    info.imports.append(Imp(module, level, "*", None, _line(c)))
        elif t == "call":
            fn = child.child_by_field_name("function")
            if fn is not None and fn_stack:
                caller = ".".join([*class_stack, *fn_stack]) if class_stack else ".".join(fn_stack)
                if fn.type == "identifier":
                    info.calls.append(Call(caller, "name", None, _text(fn), _line(child)))
                elif fn.type == "attribute":
                    obj = fn.child_by_field_name("object")
                    attr = _text(fn.child_by_field_name("attribute"))
                    if obj is not None and attr:
                        if obj.type == "identifier":
                            if _text(obj) == "self":
                                info.calls.append(Call(caller, "self", None, attr, _line(child)))
                            else:
                                info.calls.append(Call(caller, "mod", _text(obj), attr, _line(child)))
            _walk_python(child, info, class_stack, fn_stack)
        else:
            _walk_python(child, info, class_stack, fn_stack)


# ── typescript / javascript AST walk ────────────────────────────────────────

_TS_FN_VALUE_TYPES = ("arrow_function", "function_expression", "generator_function")


def _ts_class_bases(node: Node) -> list[str]:
    bases: list[str] = []
    for c in node.children:
        if c.type == "class_heritage":
            stack = list(c.children)
            while stack:
                n = stack.pop(0)
                if n.type in ("identifier", "member_expression", "type_identifier"):
                    bases.append(_text(n).split("<")[0].split(".")[-1])
                    break
                stack = list(n.children) + stack
    return bases


def _walk_ts(node: Node, info: FileInfo, class_stack: list[str], fn_stack: list[str]) -> None:
    for child in node.children:
        t = child.type
        if t in ("class_declaration", "abstract_class_declaration"):
            name = _text(child.child_by_field_name("name")) or "<anonymous>"
            qname = ".".join([*class_stack, name])
            info.classes.append(ClassDef(name, qname, _line(child), _ts_class_bases(child)))
            body = child.child_by_field_name("body")
            if body is not None:
                _walk_ts(body, info, [*class_stack, name], fn_stack)
        elif t in ("function_declaration", "generator_function_declaration"):
            name = _text(child.child_by_field_name("name"))
            if name:
                qname = ".".join([*class_stack, *fn_stack, name])
                info.functions.append(FnDef(name, qname, _line(child),
                                            class_stack[-1] if class_stack else None))
                body = child.child_by_field_name("body")
                if body is not None:
                    _walk_ts(body, info, class_stack, [*fn_stack, name])
            else:
                _walk_ts(child, info, class_stack, fn_stack)
        elif t == "method_definition":
            name = _text(child.child_by_field_name("name"))
            if name:
                # class methods: Cls.m; object-literal methods inside a
                # function: outer.m — must match the caller-qname convention
                qname = ".".join([*class_stack, *fn_stack, name])
                info.functions.append(FnDef(name, qname, _line(child),
                                            class_stack[-1] if class_stack else None))
                body = child.child_by_field_name("body")
                if body is not None:
                    _walk_ts(body, info, class_stack, [*fn_stack, name])
            else:
                _walk_ts(child, info, class_stack, fn_stack)
        elif t == "variable_declarator":
            name_node = child.child_by_field_name("name")
            value = child.child_by_field_name("value")
            if (name_node is not None and name_node.type == "identifier"
                    and value is not None and value.type in _TS_FN_VALUE_TYPES):
                name = _text(name_node)
                qname = ".".join([*class_stack, *fn_stack, name])
                info.functions.append(FnDef(name, qname, _line(child),
                                            class_stack[-1] if class_stack else None))
                _walk_ts(value, info, class_stack, [*fn_stack, name])
            else:
                _walk_ts(child, info, class_stack, fn_stack)
        elif t == "import_statement":
            src = child.child_by_field_name("source")
            spec = _text(src).strip("'\"") if src is not None else ""
            if spec:
                clause = next((c for c in child.named_children if c.type == "import_clause"), None)
                bound = False
                if clause is not None:
                    for c in clause.named_children:
                        if c.type == "identifier":  # default import
                            info.imports.append(Imp(spec, 0, "default", _text(c), _line(child)))
                            bound = True
                        elif c.type == "namespace_import":
                            ident = next((n for n in c.named_children if n.type == "identifier"), None)
                            if ident is not None:
                                info.imports.append(Imp(spec, 0, None, _text(ident), _line(child)))
                                bound = True
                        elif c.type == "named_imports":
                            for s in c.named_children:
                                if s.type == "import_specifier":
                                    n = _text(s.child_by_field_name("name"))
                                    a = s.child_by_field_name("alias")
                                    info.imports.append(
                                        Imp(spec, 0, n, _text(a) if a is not None else n, _line(child)))
                                    bound = True
                if not bound:  # side-effect import
                    info.imports.append(Imp(spec, 0, None, None, _line(child)))
        elif t == "call_expression":
            fn = child.child_by_field_name("function")
            if fn is not None and fn_stack:
                caller = ".".join([*class_stack, *fn_stack]) if class_stack else ".".join(fn_stack)
                if fn.type == "identifier":
                    info.calls.append(Call(caller, "name", None, _text(fn), _line(child)))
                elif fn.type == "member_expression":
                    obj = fn.child_by_field_name("object")
                    prop = _text(fn.child_by_field_name("property"))
                    if obj is not None and prop:
                        if obj.type == "this":
                            info.calls.append(Call(caller, "self", None, prop, _line(child)))
                        elif obj.type == "identifier":
                            info.calls.append(Call(caller, "mod", _text(obj), prop, _line(child)))
            _walk_ts(child, info, class_stack, fn_stack)
        elif t in ("jsx_opening_element", "jsx_self_closing_element"):
            name_node = child.child_by_field_name("name")
            if name_node is not None and name_node.type == "identifier" and fn_stack:
                name = _text(name_node)
                if name[:1].isupper():  # component usage, not <div>
                    caller = ".".join([*class_stack, *fn_stack]) if class_stack else ".".join(fn_stack)
                    info.calls.append(Call(caller, "name", None, name, _line(child)))
            _walk_ts(child, info, class_stack, fn_stack)
        else:
            _walk_ts(child, info, class_stack, fn_stack)


# ── import resolution ───────────────────────────────────────────────────────


@dataclass
class Binding:
    """What a local imported name refers to."""
    kind: str                 # "file" | "module"
    relpath: str | None       # resolved repo-relative file, if kind == "file"
    symbol: str | None        # symbol within that file (None => whole module)
    module: str               # original module string (for Module node ids)


def _resolve_py_module(relpath: str, module: str, level: int,
                       comp: Component, fileset: set[str]) -> str | None:
    parts = [p for p in module.split(".") if p] if module else []
    roots: list[PurePosixPath] = []
    if level > 0:
        base = PurePosixPath(relpath).parent
        for _ in range(level - 1):
            base = base.parent
        roots = [base]
    else:
        roots = [PurePosixPath(comp.path), PurePosixPath(".")]
    for root in roots:
        p = root
        for part in parts:
            p = p / part
        candidates = [f"{p}.py", str(p / "__init__.py")] if parts else [str(p / "__init__.py")]
        for cand in candidates:
            cand = str(PurePosixPath(cand))
            if cand.startswith("./"):
                cand = cand[2:]
            if cand in fileset:
                return cand
    return None


def _resolve_ts_spec(relpath: str, spec: str, comp: Component, fileset: set[str]) -> str | None:
    if spec.startswith("."):
        base = PurePosixPath(relpath).parent / spec
    elif spec.startswith("@/"):
        base = PurePosixPath(comp.path) / spec[2:]
    else:
        return None
    # normalize ../ segments
    norm: list[str] = []
    for part in str(base).split("/"):
        if part == "..":
            if norm:
                norm.pop()
        elif part not in (".", ""):
            norm.append(part)
    p = "/".join(norm)
    if p in fileset:
        return p
    for ext in _TS_EXT_CANDIDATES:
        if p + ext in fileset:
            return p + ext
    for ext in _TS_EXT_CANDIDATES:
        idx = f"{p}/index{ext}"
        if idx in fileset:
            return idx
    return None


# ── the extractor ───────────────────────────────────────────────────────────


class StructureExtractor:
    name = "structure"

    def extract(self, target: TargetConfig) -> Facts:
        facts = Facts(extractor=self.name, target=target.name)
        languages = _languages()
        parsers = {lang: Parser(l) for lang, l in languages.items()}

        target_id = f"target:{target.name}"
        facts.add_node(target_id, "Target", name=target.name, root=str(target.root))

        # component (longest path prefix wins if nested) and file discovery
        files: dict[str, Component] = {}
        for comp in sorted(target.components, key=lambda c: len(c.path)):
            comp_id = f"component:{comp.name}"
            facts.add_node(comp_id, "Component", name=comp.name, path=comp.path,
                           lang=comp.lang, framework=comp.framework)
            facts.add_edge(target_id, comp_id, "CONTAINS")
            for f in self._walk_files(target, comp):
                files[f] = comp

        infos: dict[str, FileInfo] = {}
        for relpath in sorted(files):
            comp = files[relpath]
            ext = PurePosixPath(relpath).suffix
            lang = LANG_BY_EXT[ext]
            try:
                data = (target.root / relpath).read_bytes()
            except OSError as e:
                log.warning("cannot read %s: %s", relpath, e)
                continue
            info = FileInfo(relpath=relpath, component=comp.name, lang=lang)
            tree = parsers[lang].parse(data)
            if lang == "python":
                _walk_python(tree.root_node, info, [], [])
            else:
                _walk_ts(tree.root_node, info, [], [])
            infos[relpath] = info

        fileset = set(infos)
        for relpath, info in infos.items():
            fid = f"file:{relpath}"
            facts.add_node(fid, "File", name=PurePosixPath(relpath).name, path=relpath,
                           lang=info.lang, component=info.component)
            facts.add_edge(f"component:{info.component}", fid, "CONTAINS")
            for c in info.classes:
                cid = f"class:{relpath}::{c.qname}"
                facts.add_node(cid, "Class", name=c.name, qname=c.qname,
                               path=relpath, line=c.line)
                facts.add_edge(fid, cid, "DEFINES")
            for f in info.functions:
                fnid = f"fn:{relpath}::{f.qname}"
                facts.add_node(fnid, "Function", name=f.name, qname=f.qname,
                               path=relpath, line=f.line)
                if f.class_name is not None:
                    facts.add_edge(f"class:{relpath}::{self._class_qname(info, f.class_name)}",
                                   fnid, "DEFINES")
                else:
                    facts.add_edge(fid, fnid, "DEFINES")

        bindings = self._resolve_imports(target, infos, files, facts)
        self._resolve_calls(infos, bindings, facts)
        self._resolve_inheritance(infos, bindings, facts)
        return facts

    # -- discovery ----------------------------------------------------------

    def _walk_files(self, target: TargetConfig, comp: Component) -> list[str]:
        excluded = set(target.exclude)
        out: list[str] = []
        comp_root = target.component_root(comp)

        def rec(d: Path) -> None:
            try:
                entries = sorted(d.iterdir(), key=lambda p: p.name)
            except OSError as e:
                log.warning("cannot list %s: %s", d, e)
                return
            for p in entries:
                if p.name in excluded or p.name.startswith("."):
                    continue  # hidden dirs (.next, .git, …) are build/VCS caches
                if p.is_symlink():
                    continue
                if p.is_dir():
                    rec(p)
                elif p.suffix in LANG_BY_EXT:
                    try:
                        if p.stat().st_size > MAX_FILE_BYTES:
                            log.info("skipping oversized file %s", p)
                            continue
                    except OSError:
                        continue
                    out.append(p.relative_to(target.root).as_posix())

        rec(comp_root)
        return out

    # -- resolution passes ---------------------------------------------------

    @staticmethod
    def _class_qname(info: FileInfo, class_name: str) -> str:
        c = info.cls(class_name)
        return c.qname if c is not None else class_name

    def _resolve_imports(self, target: TargetConfig, infos: dict[str, FileInfo],
                         files: dict[str, Component], facts: Facts,
                         ) -> dict[str, dict[str, Binding]]:
        fileset = set(infos)
        bindings: dict[str, dict[str, Binding]] = {}
        for relpath, info in infos.items():
            comp = files[relpath]
            fid = f"file:{relpath}"
            fb: dict[str, Binding] = {}
            for imp in info.imports:
                if info.lang == "python":
                    resolved = _resolve_py_module(relpath, imp.module, imp.level, comp, fileset)
                    display = ("." * imp.level) + imp.module
                    if imp.symbol is None:  # plain `import x[.y] [as z]`
                        if resolved:
                            facts.add_edge(fid, f"file:{resolved}", "IMPORTS")
                            if imp.alias:
                                fb[imp.alias] = Binding("file", resolved, None, display)
                        else:
                            mid = f"module:{display}"
                            facts.add_node(mid, "Module", name=display)
                            facts.add_edge(fid, mid, "IMPORTS")
                            if imp.alias:
                                fb[imp.alias] = Binding("module", None, None, display)
                    else:  # from X import Y
                        sub = None
                        if imp.symbol != "*":
                            sub = _resolve_py_module(
                                relpath,
                                (imp.module + "." + imp.symbol).lstrip("."),
                                imp.level, comp, fileset)
                        if sub:
                            facts.add_edge(fid, f"file:{sub}", "IMPORTS")
                            if imp.alias:
                                fb[imp.alias] = Binding("file", sub, None, display)
                        elif resolved:
                            facts.add_edge(fid, f"file:{resolved}", "IMPORTS")
                            if imp.alias and imp.symbol != "*":
                                fb[imp.alias] = Binding("file", resolved, imp.symbol, display)
                        else:
                            mid = f"module:{display}"
                            facts.add_node(mid, "Module", name=display)
                            facts.add_edge(fid, mid, "IMPORTS")
                            if imp.alias and imp.symbol != "*":
                                fb[imp.alias] = Binding("module", None, imp.symbol, display)
                else:  # TS / TSX / JS
                    resolved = _resolve_ts_spec(relpath, imp.module, comp, fileset)
                    if resolved:
                        facts.add_edge(fid, f"file:{resolved}", "IMPORTS")
                        if imp.alias:
                            fb[imp.alias] = Binding("file", resolved, imp.symbol, imp.module)
                    else:
                        mid = f"module:{imp.module}"
                        facts.add_node(mid, "Module", name=imp.module)
                        facts.add_edge(fid, mid, "IMPORTS")
                        if imp.alias:
                            fb[imp.alias] = Binding("module", None, imp.symbol, imp.module)
            bindings[relpath] = fb
        return bindings

    def _resolve_calls(self, infos: dict[str, FileInfo],
                       bindings: dict[str, dict[str, Binding]], facts: Facts) -> None:
        for relpath, info in infos.items():
            fb = bindings.get(relpath, {})
            for call in info.calls:
                src = f"fn:{relpath}::{call.caller_qname}"
                dst = self._resolve_call_target(info, infos, fb, call)
                if dst is not None:
                    facts.add_edge(src, dst, "CALLS", line=call.line)

    def _resolve_call_target(self, info: FileInfo, infos: dict[str, FileInfo],
                             fb: dict[str, Binding], call: Call) -> str | None:
        if call.kind == "name":
            local = info.any_fn(call.callee)
            if local is not None:
                return f"fn:{info.relpath}::{local.qname}"
            b = fb.get(call.callee)
            if b is not None and b.kind == "file" and b.relpath in infos:
                rinfo = infos[b.relpath]
                sym = b.symbol or call.callee
                if sym == "default":  # `import Foo from './x'` — match by local name
                    sym = call.callee
                fn = rinfo.top_level_fn(sym)
                if fn is not None:
                    return f"fn:{b.relpath}::{fn.qname}"
        elif call.kind == "self":
            # innermost class owning the caller
            cls = call.caller_qname.rsplit(".", 2)[0] if "." in call.caller_qname else None
            if cls:
                m = info.method(cls.split(".")[-1], call.callee)
                if m is not None:
                    return f"fn:{info.relpath}::{m.qname}"
        elif call.kind == "mod" and call.obj is not None:
            # Class.method() on a local class
            local_cls = info.cls(call.obj)
            if local_cls is not None:
                m = info.method(call.obj, call.callee)
                if m is not None:
                    return f"fn:{info.relpath}::{m.qname}"
            b = fb.get(call.obj)
            if b is not None and b.kind == "file" and b.relpath in infos:
                rinfo = infos[b.relpath]
                if b.symbol is None:  # module import: m.f()
                    fn = rinfo.top_level_fn(call.callee)
                    if fn is not None:
                        return f"fn:{b.relpath}::{fn.qname}"
                else:  # imported class: Class.method()
                    m = rinfo.method(b.symbol, call.callee)
                    if m is not None:
                        return f"fn:{b.relpath}::{m.qname}"
        return None

    def _resolve_inheritance(self, infos: dict[str, FileInfo],
                             bindings: dict[str, dict[str, Binding]], facts: Facts) -> None:
        # global class-name index for unique-name fallback
        by_name: dict[str, list[tuple[str, str]]] = {}
        for relpath, info in infos.items():
            for c in info.classes:
                by_name.setdefault(c.name, []).append((relpath, c.qname))

        for relpath, info in infos.items():
            fb = bindings.get(relpath, {})
            for c in info.classes:
                src = f"class:{relpath}::{c.qname}"
                for base in c.bases:
                    dst = None
                    local = info.cls(base)
                    if local is not None:
                        dst = f"class:{relpath}::{local.qname}"
                    else:
                        b = fb.get(base)
                        if b is not None and b.kind == "file" and b.relpath in infos:
                            rc = infos[b.relpath].cls(b.symbol or base)
                            if rc is not None:
                                dst = f"class:{b.relpath}::{rc.qname}"
                        if dst is None:
                            cands = by_name.get(base, [])
                            if len(cands) == 1:
                                dst = f"class:{cands[0][0]}::{cands[0][1]}"
                    if dst is not None and dst != src:
                        facts.add_edge(src, dst, "INHERITS")
