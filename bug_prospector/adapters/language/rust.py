from __future__ import annotations

import re
from pathlib import Path

from .base import LanguageAdapter
from ...core.models.record import TestCaseResult


class RustAdapter(LanguageAdapter):

    @property
    def language_name(self) -> str:
        return "rust"

    def detect(self, repo_path: Path) -> bool:
        return (repo_path / "Cargo.toml").exists()

    def default_test_patterns(self) -> list[str]:
        return ["tests/**", "**/tests/*.rs"]

    def default_test_command(self, repo_path: Path) -> str:
        return "cargo test"

    def default_setup_commands(self, repo_path: Path) -> list[str]:
        return ["cargo build --quiet"]

    def parse_test_output(self, stdout: str, stderr: str) -> list[TestCaseResult]:
        """cargo test の出力からテストケース単体の結果を抽出する。

        verbose:  "test path::name ... ok"  /  "test path::name ... FAILED"
                  "test path::name - should panic ... ok"  (パニックテスト)
                  "test src/lib.rs - (line N) ... ok"  (doctest)
        example:  "Testing examples/foo.md:N ... ok 8ms 467us 700ns"
        quiet:    失敗名のみ failures: セクションに列挙される
        """
        results: list[TestCaseResult] = []
        combined = stdout + "\n" + stderr
        lines = combined.splitlines()

        for line in lines:
            # verbose 出力: "test <name> ... ok/FAILED/ignored [(duration)]"
            # (.+?) を使うことでスペースを含む名前（should panic, doctest等）にも対応
            m = re.match(r'^test (.+?) \.\.\. (ok|FAILED|ignored)(?:\s+\((.+?)\))?', line)
            if m:
                status, dur_str = m.group(2), m.group(3)
                if status == "ignored":
                    continue
                duration = _parse_duration(dur_str) if dur_str else None
                results.append(TestCaseResult(
                    name=m.group(1),
                    passed=status == "ok",
                    duration_seconds=duration,
                ))
                continue

            # example/doc-test 出力: "Testing <path> ... ok/failed/ignored [timing]"
            # "failed" は小文字（"test" 行の "FAILED" と異なる）
            # タイミングは "8ms 467us 700ns" 形式（括弧なし）なので duration は取得しない
            m = re.match(r'^Testing (\S+) \.\.\. (ok|failed|ignored)', line)
            if m:
                status = m.group(2)
                if status == "ignored":
                    continue
                results.append(TestCaseResult(
                    name=f"example::{m.group(1)}",
                    passed=status == "ok",
                ))
                continue

            # quiet 失敗割込み: "name --- FAILED" (progress dots 途中に挿入される)
            m = re.match(r'^(\S+) --- FAILED$', line)
            if m:
                results.append(TestCaseResult(name=m.group(1), passed=False))

        if results:
            return results

        # quiet 出力: failures: セクションの末尾サマリーから失敗名を取得
        # フォーマット:
        #   failures:
        #       test_name_1
        #       test_name_2
        # （stdout セクション付きの最初の failures: ではなく、末尾のサマリーを対象とする）
        last_failures_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == "failures:":
                last_failures_idx = i

        if last_failures_idx >= 0:
            for line in lines[last_failures_idx + 1:]:
                name = line.strip()
                if not name:
                    break
                # "---- name stdout ----" はスキップ（最初の failures: セクション内）
                if name.startswith("----"):
                    break
                results.append(TestCaseResult(name=name, passed=False))

        return results


def _parse_duration(s: str) -> float | None:
    """'1.23s' や '450ms' を秒数に変換する。"""
    m = re.match(r'^([\d.]+)(ms|s)$', s)
    if not m:
        return None
    value = float(m.group(1))
    return value / 1000.0 if m.group(2) == "ms" else value