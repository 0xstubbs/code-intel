"""Extractor registry. Adding an analysis = one module + one line here.

Configured extractor names missing from REGISTRY are skipped with a warning
at index time (other workers land them incrementally); names outside
config.KNOWN_EXTRACTORS are a hard config error.
"""

from codeintel.extractors.base import Extractor, Facts, normalize_facts, write_facts
from codeintel.extractors.structure import StructureExtractor

REGISTRY: dict[str, Extractor] = {
    "structure": StructureExtractor(),
}

__all__ = ["REGISTRY", "Extractor", "Facts", "normalize_facts", "write_facts"]
