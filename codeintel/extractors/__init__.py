"""Extractor registry. Adding an analysis = one module + one line here.

Configured extractor names missing from REGISTRY are skipped with a warning
at index time (other workers land them incrementally); names outside
config.KNOWN_EXTRACTORS are a hard config error.
"""

import importlib

from codeintel.extractors.base import Extractor, Facts, normalize_facts, write_facts
from codeintel.extractors.structure import StructureExtractor

REGISTRY: dict[str, Extractor] = {
    "structure": StructureExtractor(),
}


def _register_optional(name: str, module: str, cls: str) -> None:
    """Pre-wired slots for extractors that land incrementally (W2/W3).

    Workers add a module without touching this file; absence is not an error.
    """
    try:
        mod = importlib.import_module(module)
    except ImportError:
        return
    REGISTRY[name] = getattr(mod, cls)()


_register_optional("python-api", "codeintel.extractors.python_api", "PythonApiExtractor")
_register_optional("nextjs", "codeintel.extractors.nextjs", "NextjsExtractor")
_register_optional("seams", "codeintel.extractors.seams", "SeamsExtractor")
_register_optional("dependencies", "codeintel.extractors.dependencies", "DependenciesExtractor")
_register_optional("infra", "codeintel.extractors.infra", "InfraExtractor")
_register_optional("tests", "codeintel.extractors.tests", "TestsExtractor")
_register_optional("git-meta", "codeintel.extractors.git_meta", "GitMetaExtractor")
_register_optional("similarity", "codeintel.extractors.similarity", "SimilarityExtractor")

__all__ = ["REGISTRY", "Extractor", "Facts", "normalize_facts", "write_facts"]
