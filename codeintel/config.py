"""Target configuration loader.

A target is a YAML file under targets/<name>.yaml — never code. See
CONTRACTS.md "Target config".
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path

import yaml

#: Extractor names admissible in a target config (CONTRACTS.md v1).
#: This is the *contract* vocabulary; the runtime REGISTRY
#: (codeintel.extractors.REGISTRY) may implement only a subset — configured
#: extractors missing from the registry are skipped with a warning at index
#: time, but names outside this set are a hard config error.
KNOWN_EXTRACTORS = frozenset(
    {
        "structure",
        "python-api",
        "nextjs",
        "seams",
        "similarity",
        "dependencies",
        "infra",
        "tests",
        "git-meta",
    }
)


class ConfigError(Exception):
    """Raised loudly for malformed or invalid target configs."""


@dataclass(frozen=True)
class Component:
    name: str
    path: str  # relative to target root
    lang: str
    framework: str | None = None


@dataclass(frozen=True)
class TargetConfig:
    name: str
    root: Path  # absolute; the platform NEVER writes under root
    exclude: tuple[str, ...] = ()
    components: tuple[Component, ...] = ()
    extractors: tuple[str, ...] = ()
    source: Path | None = None  # the yaml file this was loaded from

    def component_root(self, component: Component) -> Path:
        return self.root / component.path


def _require(mapping: dict, key: str, ctx: str):
    if key not in mapping:
        raise ConfigError(f"{ctx}: missing required key '{key}'")
    return mapping[key]


def load_target(yaml_path: Path) -> TargetConfig:
    """Load and validate a target config. Raises ConfigError loudly."""
    yaml_path = Path(yaml_path)
    if not yaml_path.exists():
        raise ConfigError(f"target config not found: {yaml_path}")
    try:
        raw = yaml.safe_load(yaml_path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{yaml_path}: invalid YAML: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{yaml_path}: top level must be a mapping")

    ctx = str(yaml_path)
    name = _require(raw, "name", ctx)
    root = Path(_require(raw, "root", ctx))
    if not root.is_absolute():
        raise ConfigError(f"{ctx}: root must be an absolute path, got '{root}'")
    if not root.is_dir():
        raise ConfigError(f"{ctx}: root does not exist or is not a directory: {root}")

    exclude = raw.get("exclude", []) or []
    if not isinstance(exclude, list):
        raise ConfigError(f"{ctx}: 'exclude' must be a list")

    components: list[Component] = []
    for i, c in enumerate(raw.get("components", []) or []):
        cctx = f"{ctx}: components[{i}]"
        if not isinstance(c, dict):
            raise ConfigError(f"{cctx}: must be a mapping")
        known = {f.name for f in dataclasses.fields(Component)}
        extra = set(c) - known
        if extra:
            raise ConfigError(f"{cctx}: unknown keys {sorted(extra)}")
        comp = Component(
            name=_require(c, "name", cctx),
            path=_require(c, "path", cctx),
            lang=_require(c, "lang", cctx),
            framework=c.get("framework"),
        )
        if not (root / comp.path).is_dir():
            raise ConfigError(f"{cctx}: path '{comp.path}' not found under {root}")
        components.append(comp)

    extractors = raw.get("extractors", []) or []
    if not isinstance(extractors, list):
        raise ConfigError(f"{ctx}: 'extractors' must be a list")
    unknown = [e for e in extractors if e not in KNOWN_EXTRACTORS]
    if unknown:
        raise ConfigError(
            f"{ctx}: unknown extractor name(s) {unknown}. "
            f"Known extractors: {sorted(KNOWN_EXTRACTORS)}"
        )

    return TargetConfig(
        name=str(name),
        root=root,
        exclude=tuple(str(e) for e in exclude),
        components=tuple(components),
        extractors=tuple(str(e) for e in extractors),
        source=yaml_path,
    )


def load_target_by_name(name: str, targets_dir: Path) -> TargetConfig:
    return load_target(Path(targets_dir) / f"{name}.yaml")


def list_targets(targets_dir: Path) -> list[Path]:
    d = Path(targets_dir)
    if not d.is_dir():
        return []
    return sorted(d.glob("*.yaml"))
