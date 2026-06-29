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

        パニック等でスレッドが異常終了した場合、verbose の "... FAILED" 行が出力されず
        末尾の failures: サマリーにのみ名前が現れることがある。
        このため verbose 解析と failures サマリー解析を常に両方実行し、
        サマリーに存在するが verbose で捕捉できなかった失敗を補完する。
        """
        results: list[TestCaseResult] = []
        seen: set[str] = set()
        combined = stdout + "\n" + stderr
        lines = combined.splitlines()

        for line in lines:
            # verbose 出力: "test <name> ... ok/FAILED/ignored [(duration)]"
            m = re.match(r'^test (.+?) \.\.\. (ok|FAILED|ignored)(?:\s+\((.+?)\))?', line)
            if m:
                status, dur_str = m.group(2), m.group(3)
                if status == "ignored":
                    continue
                duration = _parse_duration(dur_str) if dur_str else None
                name = m.group(1)
                seen.add(name)
                results.append(TestCaseResult(
                    name=name,
                    passed=status == "ok",
                    duration_seconds=duration,
                ))
                continue

            # example/doc-test 出力: "Testing <path> ... ok/failed/ignored [timing]"
            m = re.match(r'^Testing (\S+) \.\.\. (ok|failed|ignored)', line)
            if m:
                status = m.group(2)
                if status == "ignored":
                    continue
                name = f"example::{m.group(1)}"
                seen.add(name)
                results.append(TestCaseResult(name=name, passed=status == "ok"))
                continue

            # quiet 失敗割込み: "name --- FAILED" (progress dots 途中に挿入される)
            m = re.match(r'^(\S+) --- FAILED$', line)
            if m:
                name = m.group(1)
                seen.add(name)
                results.append(TestCaseResult(name=name, passed=False))

        # failures: サマリーセクションから失敗名を補完する。
        # verbose 解析で捕捉済みのものはスキップし、未捕捉のものだけ追加する。
        # 出力に failures: ブロックが複数ある場合（stdout ダンプ付きの先頭ブロック＋末尾サマリー）、
        # 末尾（最後の）failures: を対象とする。
        last_failures_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == "failures:":
                last_failures_idx = i

        if last_failures_idx >= 0:
            for line in lines[last_failures_idx + 1:]:
                name = line.strip()
                if not name:
                    break
                # "---- name stdout ----" が来たら stdout ダンプ付きブロックなので終了
                if name.startswith("----"):
                    break
                if name not in seen:
                    seen.add(name)
                    results.append(TestCaseResult(name=name, passed=False))

        return results


def _parse_duration(s: str) -> float | None:
    """'1.23s' や '450ms' を秒数に変換する。"""
    m = re.match(r'^([\d.]+)(ms|s)$', s)
    if not m:
        return None
    value = float(m.group(1))
    return value / 1000.0 if m.group(2) == "ms" else value