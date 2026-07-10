"""Git metadata patch extractor for File nodes."""

from __future__ import annotations

import logging
import subprocess
from datetime import date, datetime, timezone
from pathlib import PurePosixPath

from codeintel.config import TargetConfig
from codeintel.extractors.base import Facts

log = logging.getLogger(__name__)


class GitMetaExtractor:
    name = "git-meta"

    def extract(self, target: TargetConfig) -> Facts:
        facts = Facts(extractor=self.name, target=target.name)
        paths = _component_paths(target)
        if not paths:
            log.warning("target %s has no component paths for git metadata", target.name)
            return facts
        try:
            proc = subprocess.run(
                ["git", "-C", str(target.root), "log", "--format=%H|%cI", "--name-only", "--", *paths],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as e:
            log.warning("cannot run git log for %s: %s", target.root, e)
            return facts
        if proc.returncode != 0:
            log.warning("git log failed for %s: %s", target.root, proc.stderr.strip())
            return facts

        cutoff = date.today().replace(year=date.today().year - 1)
        meta: dict[str, dict[str, object]] = {}
        current_sha: str | None = None
        current_date: date | None = None
        for raw in proc.stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            if "|" in line and len(line.split("|", 1)[0]) == 40:
                sha, iso = line.split("|", 1)
                current_sha = sha
                try:
                    current_date = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc).date()
                except ValueError:
                    current_date = None
                continue
            if current_sha is None or current_date is None or not _under_component(line, paths):
                continue
            item = meta.setdefault(line, {"commit_count_12mo": 0})
            if "last_commit_sha" not in item:
                item["last_commit_sha"] = current_sha[:12]
                item["last_commit_date"] = current_date.isoformat()
            if current_date >= cutoff:
                item["commit_count_12mo"] = int(item["commit_count_12mo"]) + 1

        for relpath in sorted(meta):
            facts.add_node(f"file:{relpath}", "File", **meta[relpath])
        return facts


def _component_paths(target: TargetConfig) -> list[str]:
    return sorted({str(PurePosixPath(c.path)) for c in target.components if c.path})


def _under_component(relpath: str, paths: list[str]) -> bool:
    return any(relpath == p or relpath.startswith(p + "/") for p in paths)
