from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class FileCoverage:
    """1ファイル分のカバレッジ情報。行番号は1始まり。"""
    path: str  # リポジトリルートからの相対パス（"/" 区切り）
    instrumented_lines: set[int] = field(default_factory=set)  # 実行可能コードのある行
    covered_lines: set[int] = field(default_factory=set)       # そのうちテストで実行された行


@dataclass
class ParsedCoverage:
    files: dict[str, FileCoverage] = field(default_factory=dict)
    overall_line_rate: float | None = None
    overall_branch_rate: float | None = None


class CoverageAdapter(ABC):

    @property
    @abstractmethod
    def tool_name(self) -> str: ...

    @abstractmethod
    def default_command(self, repo_path: Path) -> str:
        """カバレッジ計測込みでテストスイートを実行するコマンドを返す。"""

    @abstractmethod
    def default_report_path(self, repo_path: Path) -> Path:
        """コマンド実行後に生成されるレポートファイルの絶対パスを返す。"""

    @abstractmethod
    def parse_report(self, report_path: Path) -> ParsedCoverage:
        """カバレッジレポートを解析する。"""
