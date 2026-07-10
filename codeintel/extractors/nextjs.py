"""Next.js app-router extractor."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from codeintel.config import Component, TargetConfig
from codeintel.extractors._http import (
    HTTP_METHODS,
    app_route_url,
    component_root,
    next_api_to_backend,
    page_id,
    proxy_id,
    repo_rel,
    should_skip,
)
from codeintel.extractors.base import Facts

log = logging.getLogger(__name__)


class NextjsExtractor:
    name = "nextjs"

    def extract(self, target: TargetConfig) -> Facts:
        facts = Facts(extractor=self.name, target=target.name)
        for comp in target.components:
            if comp.framework != "nextjs":
                continue
            self._extract_component(target, comp, facts)
        return facts

    def _extract_component(self, target: TargetConfig, comp: Component, facts: Facts) -> None:
        root = component_root(target, comp)
        app_root = root / "app"
        if app_root.is_dir():
            for path in sorted(app_root.rglob("*")):
                if should_skip(path, target) or not path.is_file():
                    continue
                name = path.name
                if re.match(r"page\.(tsx|ts|jsx|js)$", name):
                    url = app_route_url(app_root, path)
                    pid = page_id(url)
                    facts.add_node(pid, "Page", path=repo_rel(target, path), line=1, route=url)
                    facts.add_edge(f"component:{comp.name}", pid, "EXPOSES")
                elif re.match(r"layout\.(tsx|ts|jsx|js)$", name):
                    url = app_route_url(app_root, path)
                    pid = page_id(url)
                    facts.add_node(pid, "Page", path=repo_rel(target, path), line=1, route=url, layout=True)
                    facts.add_edge(f"component:{comp.name}", pid, "EXPOSES")
                elif re.match(r"route\.(ts|js)$", name) and "/api/" in path.as_posix():
                    self._route_file(target, comp, app_root, path, facts)
        self._rewrites(target, comp, root, facts)

    def _route_file(self, target: TargetConfig, comp: Component, app_root: Path, path: Path, facts: Facts) -> None:
        url = app_route_url(app_root, path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log.warning("nextjs: skipping %s: %s", repo_rel(target, path), e)
            return
        methods = sorted(set(re.findall(r"export\s+(?:async\s+)?function\s+(" + "|".join(HTTP_METHODS) + r")\b", text)))
        if not methods:
            methods = sorted(set(re.findall(r"export\s+const\s+(" + "|".join(HTTP_METHODS) + r")\b", text)))
        pid = proxy_id(url)
        facts.add_node(
            pid,
            "ApiProxy",
            path=repo_rel(target, path),
            line=1,
            route=url,
            methods=",".join(methods),
        )
        facts.add_edge(f"component:{comp.name}", pid, "EXPOSES")

    def _rewrites(self, target: TargetConfig, comp: Component, root: Path, facts: Facts) -> None:
        config = root / "next.config.ts"
        if not config.exists():
            config = root / "next.config.js"
        if not config.exists():
            return
        try:
            text = config.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            log.warning("nextjs: skipping %s: %s", repo_rel(target, config), e)
            return
        if 'source: "/api/:path*"' not in text and "source: '/api/:path*'" not in text:
            return
        if "/api/v1/:path*" not in text:
            return
        route = "/api/[path]"
        pid = proxy_id(route)
        facts.add_node(
            pid,
            "ApiProxy",
            path=repo_rel(target, config),
            line=1,
            route=route,
            backend_path=next_api_to_backend(route),
            methods="*",
            rewrite=True,
        )
        facts.add_edge(f"component:{comp.name}", pid, "EXPOSES")
