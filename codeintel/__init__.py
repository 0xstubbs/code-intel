"""codeintel — project-agnostic code-intelligence platform.

Extracts typed facts from target codebases (configured in targets/*.yaml)
and loads them into a unified Kuzu graph under out/<target>/.
"""

from pathlib import Path

__version__ = "0.1.0"

#: Repository root of the codeintel project itself (NOT a target root).
#: All outputs live under PROJECT_ROOT / "out"; target configs under
#: PROJECT_ROOT / "targets". Anchored to this file so the CLI works from
#: any cwd.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
TARGETS_DIR = PROJECT_ROOT / "targets"
OUT_DIR = PROJECT_ROOT / "out"
