"""Shared HTTP/path helpers for API, Next.js, and seam extractors."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

from codeintel.config import Component, TargetConfig

HTTP_METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


def should_skip(path: Path, target: TargetConfig) -> bool:
    parts = set(path.parts)
    return any(ex in parts for ex in target.exclude)


def repo_rel(target: TargetConfig, path: Path) -> str:
    return path.relative_to(target.root).as_posix()


def component_root(target: TargetConfig, component: Component) -> Path:
    return target.root / component.path


def join_url(*parts: str) -> str:
    out = ""
    for part in parts:
        if not part:
            continue
        s = str(part)
        if not out:
            out = s
        else:
            out = out.rstrip("/") + "/" + s.lstrip("/")
    if not out:
        return "/"
    if not out.startswith("/"):
        out = "/" + out
    out = re.sub(r"/+", "/", out)
    return out.rstrip("/") or "/"


_CURLY_PARAM = re.compile(r"\{[^}/]+\}")
_NEXT_PARAM = re.compile(r"\[[^]/]+\]")


def comparable_path(path: str) -> str:
    path = path.split("?", 1)[0].split("#", 1)[0]
    path = join_url(path)
    path = _CURLY_PARAM.sub("[]", path)
    path = _NEXT_PARAM.sub("[]", path)
    path = re.sub(r"/+", "/", path)
    return path.rstrip("/") or "/"


def route_id(method: str, path: str) -> str:
    return f"route:{method.upper()} {join_url(path)}"


def proxy_id(path: str) -> str:
    return f"proxy:{join_url(path)}"


def page_id(path: str) -> str:
    return f"page:{join_url(path)}"


def fn_id(relpath: str, qname: str) -> str:
    return f"fn:{relpath}::{qname}"


def app_route_url(app_root: Path, route_file: Path) -> str:
    rel = route_file.relative_to(app_root).as_posix()
    parts = list(PurePosixPath(rel).parts)
    if parts and parts[-1].split(".", 1)[0] in {"page", "layout", "route"}:
        parts = parts[:-1]
    clean: list[str] = []
    for p in parts:
        if p.startswith("(") and p.endswith(")"):
            continue
        if p.startswith("@"):
            continue
        if p == "index":
            continue
        clean.append(p)
    return join_url(*clean)


def normalize_fetch_url(raw: str) -> str | None:
    s = raw.strip().strip("'\"`")
    if not s:
        return None
    if s.startswith(("http://", "https://", "//")):
        return None
    if "${" in s:
        s = re.sub(r"\$\{[^}]+\}", "[id]", s)
    if ":" in s:
        s = re.sub(r":\w+", "[id]", s)
    s = s.split("?", 1)[0].split("#", 1)[0]
    if not s.startswith("/"):
        return None
    return join_url(s)


def next_api_to_backend(path: str, backend_prefix: str = "/api/v1") -> str | None:
    path = join_url(path)
    if not (path == "/api" or path.startswith("/api/")):
        return None
    rest = path.removeprefix("/api")
    return join_url(backend_prefix, rest)
