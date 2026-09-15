from __future__ import annotations

from pathlib import Path

from .base import CoverageAdapter, ParsedCoverage
from .cobertura import parse_cobertura


class CargoLlvmCovAdapter(CoverageAdapter):

    @property
    def tool_name(self) -> str:
        return "cargo-llvm-cov"

    def default_command(self, repo_path: Path) -> str:
        # --workspace が無いとルートパッケージのみが計装され、ワークスペース内の
        # 他クレート（例: clap の実装本体である clap_builder）が計測対象から
        # 抜け落ちる。
        return "cargo llvm-cov --workspace --cobertura --output-path target/llvm-cov-cobertura.xml"

    def default_report_path(self, repo_path: Path) -> Path:
        return repo_path / "target" / "llvm-cov-cobertura.xml"

    def parse_report(self, report_path: Path) -> ParsedCoverage:
        return parse_cobertura(report_path)
