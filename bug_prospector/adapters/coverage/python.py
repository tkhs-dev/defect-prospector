from __future__ import annotations

from pathlib import Path

from .base import CoverageAdapter, ParsedCoverage
from .cobertura import parse_cobertura


class CoveragePyAdapter(CoverageAdapter):
    """coverage.py（pytest-cov経由）。`coverage xml` の出力はCobertura互換形式。"""

    @property
    def tool_name(self) -> str:
        return "coverage.py"

    def default_command(self, repo_path: Path) -> str:
        return "pytest --cov=. --cov-report=xml:coverage.xml -q"

    def default_report_path(self, repo_path: Path) -> Path:
        return repo_path / "coverage.xml"

    def parse_report(self, report_path: Path) -> ParsedCoverage:
        return parse_cobertura(report_path)
