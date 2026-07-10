"""Function-level duplicate and near-duplicate detector.

Emits SIMILAR_TO edges between Function nodes produced by the structure
extractor. The detector is deliberately deterministic: stable file walks,
stable token normalization, exact Jaccard over 5-gram shingles, and sorted
edge output.
"""

from __future__ import annotations

import ast
import hashlib
import io
import keyword
import logging
import tokenize
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from tree_sitter import Language, Node, Parser

from codeintel.config import Component, TargetConfig
from codeintel.extractors.base import Facts

log = logging.getLogger(__name__)

MAX_FILE_BYTES = 2 * 1024 * 1024
MIN_TOKENS = 25
SHINGLE_SIZE = 5
NEAR_DUP_THRESHOLD = 0.8

LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
}

_PY_KEYWORDS = set(keyword.kwlist)
_TS_IDENTIFIER_TYPES = {
    "identifier",
    "property_identifier",
    "shorthand_property_identifier",
    "type_identifier",
    "statement_identifier",
}
_TS_STRING_TYPES = {
    "string",
    "string_fragment",
    "template_string",
    "template_substitution",
}
_TS_NUMBER_TYPES = {"number", "decimal_integer"}
_TS_SKIP_TYPES = {"comment", "ERROR"}
_TS_FN_VALUE_TYPES = ("arrow_function", "function_expression", "generator_function")


@dataclass(frozen=True)
class FunctionTokens:
    id: str
    path: str
    lang: str
    loc: int
    tokens: tuple[str, ...]
    shingles: frozenset[tuple[str, ...]]
    exact_hash: str


def _languages() -> dict[str, Language]:
    import tree_sitter_typescript

    return {
        "typescript": Language(tree_sitter_typescript.language_typescript()),
        "tsx": Language(tree_sitter_typescript.language_tsx()),
    }


def _walk_files(target: TargetConfig, comp: Component) -> list[str]:
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
                continue
            if p.is_symlink():
                continue
            if p.is_dir():
                rec(p)
                continue
            if p.suffix not in LANG_BY_EXT:
                continue
            try:
                if p.stat().st_size > MAX_FILE_BYTES:
                    log.info("skipping oversized file %s", p)
                    continue
            except OSError:
                continue
            out.append(p.relative_to(target.root).as_posix())

    rec(comp_root)
    return out


def _py_expr_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _py_expr_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _py_expr_name(node.func)
    if isinstance(node, ast.Subscript):
        return _py_expr_name(node.value)
    return ""


def _python_body_source(lines: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    body = list(node.body)
    if not body:
        return ""
    start = min(getattr(stmt, "lineno", node.lineno) for stmt in body)
    end = max(getattr(stmt, "end_lineno", getattr(stmt, "lineno", start)) for stmt in body)
    return "".join(lines[start - 1:end])


def _normalize_python_source(src: str) -> tuple[str, ...]:
    tokens: list[str] = []
    try:
        stream = tokenize.generate_tokens(io.StringIO(src).readline)
        for tok in stream:
            typ = tok.type
            val = tok.string
            if typ in {
                tokenize.ENCODING,
                tokenize.NL,
                tokenize.NEWLINE,
                tokenize.INDENT,
                tokenize.DEDENT,
                tokenize.COMMENT,
                tokenize.ENDMARKER,
            }:
                continue
            if typ == tokenize.NAME:
                tokens.append(val if val in _PY_KEYWORDS else "ID")
            elif typ == tokenize.STRING:
                tokens.append("STR")
            elif typ == tokenize.NUMBER:
                tokens.append("NUM")
            elif typ == tokenize.OP:
                tokens.append(val)
            else:
                tokens.append(val)
    except tokenize.TokenError:
        return ()
    return tuple(tokens)


class _PythonFunctionCollector(ast.NodeVisitor):
    def __init__(self, relpath: str, lines: list[str]) -> None:
        self.relpath = relpath
        self.lines = lines
        self.class_stack: list[str] = []
        self.fn_stack: list[str] = []
        self.functions: list[tuple[str, tuple[str, ...]]] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_stack.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.class_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        qname = ".".join([*self.class_stack, *self.fn_stack, node.name])
        fnid = f"fn:{self.relpath}::{qname}"
        tokens = _normalize_python_source(_python_body_source(self.lines, node))
        self.functions.append((fnid, tokens))
        self.fn_stack.append(node.name)
        for stmt in node.body:
            self.visit(stmt)
        self.fn_stack.pop()


def _python_functions(relpath: str, data: bytes) -> list[tuple[str, tuple[str, ...]]]:
    text = data.decode("utf-8", errors="replace")
    try:
        tree = ast.parse(text, filename=relpath)
    except SyntaxError as e:
        log.warning("cannot parse %s: %s", relpath, e)
        return []
    collector = _PythonFunctionCollector(relpath, text.splitlines(keepends=True))
    collector.visit(tree)
    return collector.functions


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _normalize_ts_leaf(node: Node) -> str | None:
    typ = node.type
    if typ in _TS_SKIP_TYPES:
        return None
    if typ in _TS_IDENTIFIER_TYPES:
        return "ID"
    if typ in _TS_STRING_TYPES or "string" in typ:
        return "STR"
    if typ in _TS_NUMBER_TYPES or "number" in typ:
        return "NUM"
    raw = _text(node)
    if not raw:
        return None
    if raw[0] in {"'", '"', "`"}:
        return "STR"
    if raw[0].isdigit():
        return "NUM"
    return raw


def _normalize_ts_body(node: Node | None) -> tuple[str, ...]:
    if node is None:
        return ()
    tokens: list[str] = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.child_count == 0:
            tok = _normalize_ts_leaf(n)
            if tok is not None:
                tokens.append(tok)
            continue
        stack.extend(reversed(n.children))
    return tuple(tokens)


def _walk_ts_functions(
    node: Node,
    relpath: str,
    functions: list[tuple[str, tuple[str, ...]]],
    class_stack: list[str],
    fn_stack: list[str],
) -> None:
    for child in node.children:
        typ = child.type
        if typ in ("class_declaration", "abstract_class_declaration"):
            name = _text(child.child_by_field_name("name")) or "<anonymous>"
            body = child.child_by_field_name("body")
            if body is not None:
                _walk_ts_functions(body, relpath, functions, [*class_stack, name], fn_stack)
        elif typ in ("function_declaration", "generator_function_declaration"):
            name = _text(child.child_by_field_name("name"))
            if name:
                qname = ".".join([*class_stack, *fn_stack, name])
                body = child.child_by_field_name("body")
                functions.append((f"fn:{relpath}::{qname}", _normalize_ts_body(body)))
                if body is not None:
                    _walk_ts_functions(body, relpath, functions, class_stack, [*fn_stack, name])
            else:
                _walk_ts_functions(child, relpath, functions, class_stack, fn_stack)
        elif typ == "method_definition":
            name = _text(child.child_by_field_name("name"))
            if name:
                qname = ".".join([*class_stack, *fn_stack, name])
                body = child.child_by_field_name("body")
                functions.append((f"fn:{relpath}::{qname}", _normalize_ts_body(body)))
                if body is not None:
                    _walk_ts_functions(body, relpath, functions, class_stack, [*fn_stack, name])
            else:
                _walk_ts_functions(child, relpath, functions, class_stack, fn_stack)
        elif typ == "variable_declarator":
            name_node = child.child_by_field_name("name")
            value = child.child_by_field_name("value")
            if (
                name_node is not None
                and name_node.type == "identifier"
                and value is not None
                and value.type in _TS_FN_VALUE_TYPES
            ):
                name = _text(name_node)
                qname = ".".join([*class_stack, *fn_stack, name])
                body = value.child_by_field_name("body")
                functions.append((f"fn:{relpath}::{qname}", _normalize_ts_body(body)))
                _walk_ts_functions(value, relpath, functions, class_stack, [*fn_stack, name])
            else:
                _walk_ts_functions(child, relpath, functions, class_stack, fn_stack)
        else:
            _walk_ts_functions(child, relpath, functions, class_stack, fn_stack)


def _ts_functions(relpath: str, data: bytes, parser: Parser) -> list[tuple[str, tuple[str, ...]]]:
    tree = parser.parse(data)
    functions: list[tuple[str, tuple[str, ...]]] = []
    _walk_ts_functions(tree.root_node, relpath, functions, [], [])
    return functions


def _shingles(tokens: tuple[str, ...]) -> frozenset[tuple[str, ...]]:
    if len(tokens) < SHINGLE_SIZE:
        return frozenset()
    return frozenset(tokens[i:i + SHINGLE_SIZE] for i in range(len(tokens) - SHINGLE_SIZE + 1))


def _function_tokens(fnid: str, path: str, lang: str, tokens: tuple[str, ...]) -> FunctionTokens | None:
    if len(tokens) < MIN_TOKENS:
        return None
    encoded = "\x1f".join(tokens).encode("utf-8")
    return FunctionTokens(
        id=fnid,
        path=path,
        lang=lang,
        loc=len(tokens),
        tokens=tokens,
        shingles=_shingles(tokens),
        exact_hash=hashlib.sha256(encoded).hexdigest(),
    )


class SimilarityExtractor:
    name = "similarity"

    def extract(self, target: TargetConfig) -> Facts:
        facts = Facts(extractor=self.name, target=target.name)
        languages = _languages()
        parsers = {lang: Parser(parser_lang) for lang, parser_lang in languages.items()}

        files: dict[str, str] = {}
        for comp in sorted(target.components, key=lambda c: len(c.path)):
            for relpath in _walk_files(target, comp):
                files[relpath] = LANG_BY_EXT[PurePosixPath(relpath).suffix]

        functions: list[FunctionTokens] = []
        for relpath in sorted(files):
            lang = files[relpath]
            try:
                data = (target.root / relpath).read_bytes()
            except OSError as e:
                log.warning("cannot read %s: %s", relpath, e)
                continue
            try:
                raw = (
                    _python_functions(relpath, data)
                    if lang == "python"
                    else _ts_functions(relpath, data, parsers[lang])
                )
            except Exception as e:  # noqa: BLE001 - one bad file must not abort indexing
                log.warning("cannot parse %s: %s", relpath, e)
                continue
            for fnid, tokens in raw:
                fn = _function_tokens(fnid, relpath, "typescript" if lang == "tsx" else lang, tokens)
                if fn is not None:
                    functions.append(fn)

        for src, dst, score, method, loc in self._pairs(functions):
            a, b = (src, dst) if src.id < dst.id else (dst, src)
            facts.add_edge(
                a.id,
                b.id,
                "SIMILAR_TO",
                score=round(score, 2),
                method=method,
                loc=loc,
                scope="same-file" if a.path == b.path else "cross-file",
            )
        return facts

    def _pairs(
        self,
        functions: list[FunctionTokens],
    ) -> list[tuple[FunctionTokens, FunctionTokens, float, str, int]]:
        pairs: list[tuple[FunctionTokens, FunctionTokens, float, str, int]] = []
        by_lang: dict[str, list[FunctionTokens]] = {}
        for fn in sorted(functions, key=lambda f: f.id):
            by_lang.setdefault(fn.lang, []).append(fn)

        for lang in sorted(by_lang):
            fns = by_lang[lang]
            for i, src in enumerate(fns):
                for dst in fns[i + 1:]:
                    loc = min(src.loc, dst.loc)
                    if src.exact_hash == dst.exact_hash:
                        pairs.append((src, dst, 1.0, "exact", loc))
                        continue
                    if not src.shingles or not dst.shingles:
                        continue
                    inter = len(src.shingles & dst.shingles)
                    if inter == 0:
                        continue
                    union = len(src.shingles | dst.shingles)
                    score = inter / union
                    if score >= NEAR_DUP_THRESHOLD:
                        pairs.append((src, dst, score, "shingle", loc))
        return sorted(pairs, key=lambda p: (min(p[0].id, p[1].id), max(p[0].id, p[1].id)))
