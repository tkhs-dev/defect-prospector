"""
言語別カバレッジツールを実行し、diff coverage（バグ修正差分が触れた行のうち
buggyコミット時点の既存テストでカバーされていた行の割合）とプロジェクト全体の
カバレッジ率を計測する。
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

from ..adapters.coverage.base import CoverageAdapter, FileCoverage
from ..config import CoverageConfig
from .models.record import COVERAGE_ERROR, MEASURED, UNSUPPORTED, CoverageResult

logger = logging.getLogger(__name__)

_DIFF_HEADER = re.compile(r'^diff --git a/(.+?) b/(.+?)$')
_HUNK_HEADER = re.compile(r'^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@')


class CoverageRunner:

    def __init__(self, adapter: CoverageAdapter | None, config: CoverageConfig) -> None:
        self._adapter = adapter
        self._config = config

    def measure(self, repo_path: Path, unified_diff: str) -> CoverageResult:
        """repo_path（バグ状態のワークツリー）でカバレッジ計測を行う。"""
        if not self._config.enabled or self._adapter is None:
            return CoverageResult(tool=self._config.tool, status=UNSUPPORTED)

        tool = self._config.tool or self._adapter.tool_name
        command = self._config.command or self._adapter.default_command(repo_path)
        report_path = (
            repo_path / self._config.report_path
            if self._config.report_path
            else self._adapter.default_report_path(repo_path)
        )

        try:
            r = subprocess.run(
                command, shell=True, cwd=repo_path, encoding="utf-8", errors="replace",
                capture_output=True, text=True, timeout=self._config.timeout,
            )
        except subprocess.TimeoutExpired:
            return CoverageResult(tool=tool, status=COVERAGE_ERROR, error="coverage command timed out")
        except OSError as e:
            return CoverageResult(tool=tool, status=COVERAGE_ERROR, error=str(e))

        if not report_path.exists():
            logger.warning(f"  coverage report not found: {report_path}")
            # 実際のエラーはビルドログの末尾に出るため、先頭ではなく末尾を残す。
            return CoverageResult(
                tool=tool, status=COVERAGE_ERROR,
                error=(r.stderr or "coverage report not generated")[-2000:],
            )

        try:
            parsed = self._adapter.parse_report(report_path)
        except Exception as e:
            logger.warning(f"  failed to parse coverage report {report_path}: {e}")
            return CoverageResult(tool=tool, status=COVERAGE_ERROR, error=str(e)[-2000:])

        diff_total, diff_covered = _diff_coverage(unified_diff, parsed.files)

        return CoverageResult(
            tool=tool,
            status=MEASURED,
            overall_line_rate=parsed.overall_line_rate,
            overall_branch_rate=parsed.overall_branch_rate,
            diff_lines_total=diff_total,
            diff_lines_covered=diff_covered,
            diff_coverage_rate=(diff_covered / diff_total) if diff_total else None,
        )


def _diff_removed_lines(unified_diff: str) -> dict[str, set[int]]:
    """unified diff から、各ファイルで削除/変更された旧ファイル側の行番号を抽出する。

    修正差分の "a/" 側（削除・変更行）は、逆パッチ適用後のバグ状態ワークツリーに
    そのまま残っている行に対応する。
    """
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    old_lineno = 0

    for line in unified_diff.splitlines():
        m = _DIFF_HEADER.match(line)
        if m:
            current_file = m.group(1).replace("\\", "/")
            old_lineno = 0
            continue

        m = _HUNK_HEADER.match(line)
        if m:
            old_lineno = int(m.group(1))
            continue

        if current_file is None or old_lineno == 0:
            continue
        if line.startswith("---") or line.startswith("+++"):
            continue

        if line.startswith("-"):
            result.setdefault(current_file, set()).add(old_lineno)
            old_lineno += 1
        elif line.startswith("+"):
            pass  # 新ファイル側のみの行、旧ファイル（バグ状態）には存在しない
        else:
            old_lineno += 1

    return result


def _find_coverage_file(diff_path: str, files: dict[str, FileCoverage]) -> FileCoverage | None:
    """diffのファイルパスに対応する FileCoverage を探す。

    カバレッジツールによってはリポジトリルートからの完全な相対パスを持たない
    （例: JaCoCoは "com/example/Foo.java" のようにソースセット配下の相対パス）
    ため、末尾一致でも照合する。
    """
    if diff_path in files:
        return files[diff_path]

    best: FileCoverage | None = None
    best_len = -1
    for path, fc in files.items():
        if diff_path.endswith("/" + path) or path.endswith("/" + diff_path):
            if len(path) > best_len:
                best, best_len = fc, len(path)
    return best


def _diff_coverage(unified_diff: str, files: dict[str, FileCoverage]) -> tuple[int, int]:
    """修正差分が触れた行のうち、計装済みだった行数とカバーされていた行数を返す。"""
    total = 0
    covered = 0
    for diff_path, old_lines in _diff_removed_lines(unified_diff).items():
        fc = _find_coverage_file(diff_path, files)
        if fc is None:
            continue
        touched = old_lines & fc.instrumented_lines
        total += len(touched)
        covered += len(touched & fc.covered_lines)
    return total, covered
