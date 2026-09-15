from __future__ import annotations

from .base import CoverageAdapter, FileCoverage, ParsedCoverage
from .java import JacocoAdapter
from .python import CoveragePyAdapter
from .rust import CargoLlvmCovAdapter

_BY_LANGUAGE: dict[str, CoverageAdapter] = {
    "rust": CargoLlvmCovAdapter(),
    "java": JacocoAdapter(),
    "python": CoveragePyAdapter(),
}


def get_coverage_adapter(lang_type: str) -> CoverageAdapter | None:
    """言語名から対応する CoverageAdapter を返す。未対応言語は None。"""
    return _BY_LANGUAGE.get(lang_type.lower())
