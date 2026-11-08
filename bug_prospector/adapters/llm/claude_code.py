"""
`claude -p`（Claude Code CLIの非対話/printモード）をサブプロセスとして呼び出すLLMAdapter実装。

Claude Code自身がRead/Write/Edit/Bash等のツールを持つエージェントであるため、worktreeを
カレントディレクトリとして起動し、自身のツールでリポジトリを探索してテストファイルを
直接書かせる（他のLLMAdapter実装のような「プロンプト→diffテキスト」という単純な往復はしない）。

フラグの組み合わせは claude-code 2.1.290 で実機確認済み（doc/test-generation.md 6.1.1節）:
  --restricted を付けないと、--allowedTools で Write を許可しても worktree 外への書き込みを
  防げない（Writeツール自体はcwd外にも書き込めてしまう）。--restricted は同時にBash等の
  コード実行系ツールを一旦すべて外すため、テスト実行に使う分だけ --tools で戻す。
"""
from __future__ import annotations

import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from ...config import ClaudeCodeConfig
from .base import LLMAdapter, TestGenerationTask

logger = logging.getLogger(__name__)

_TASK_PROMPT = """\
You are generating a regression test that reproduces a real, already-fixed bug \
in this repository, for an automated bug-dataset collection tool. The current \
working directory is a disposable git worktree checked out at the FIX commit \
(the bug is already fixed here).

Write exactly one new test that:
  - would FAIL if the fix below were reverted (i.e. it reproduces the bug)
  - PASSES against the current (already-fixed) code in this working directory

Requirements:
  - Put the new test in a path matching one of these patterns: {test_patterns}
  - Use the project's existing test framework and conventions (feel free to \
    read existing test files under the repository for style/imports/fixtures).
  - Do not modify any file other than the new test file you create.
  - You may run the project's test command ({test_command}) to check your new \
    test passes here before finishing.

## Issue
Title: {issue_title}

{issue_body}

## Fix (source-only diff; any test changes from the original fix, if present, are omitted)
```diff
{source_diff}
```
"""

_FEEDBACK_SUFFIX = """

## Previous attempt failed
{feedback}

Revise the test file and try again. You may edit the file you created previously.
"""

# Rust向け: RUG (Cheng et al., "Turbo LLM for Rust Unit Test Generation") の核心アイデア
# ――型依存をノード単位に分解し、各ステップをコンパイラで検証してから積み上げる――を
# プロンプトでの指示として移植したもの（doc/rug-integration-note.md 参照）。
# 専用の型依存グラフ(MIRコンパイラプラグイン)は作らず、Claude Code自身のRead/Grep/Globと
# `cargo check`での自己検証に置き換えている。
_RUST_BOTTOM_UP_ADDENDUM = """

## Rust-specific guidance: build bottom-up, verify as you go

Constructing values for Rust functions (especially with generics, trait bounds, or
nested/composed types) in one shot tends to cascade into many compile errors at
once. Work bottom-up and verify incrementally instead:

1. Identify which types/values you need to construct to exercise the buggy code
   path (function arguments, trait objects, builder chains, etc).
2. For each nontrivial one, starting from the simplest/most primitive dependency
   and working up toward the type you actually need: write a small, standalone
   snippet that constructs just that value, and check it compiles on its own
   (`cargo check`) before moving on to something that depends on it. Reuse a
   snippet you already got to compile rather than re-deriving it.
3. If a type is only defined via a generic `impl<T> Trait for T where T: ...`
   ("blanket"/proxy implementation), search the crate and resolve the trait
   bounds for a concrete type that already satisfies them, rather than guessing.
4. Only assemble the final test file once each piece is known to compile on its
   own. Re-run `cargo check` as you put the pieces together.
5. If a particular piece still won't compile after a couple of tries, fall back
   to a simpler construction that still exercises the buggy code path rather
   than spending unlimited attempts on it.
"""


class ClaudeCodeAdapter(LLMAdapter):

    provider = "claude_code"

    def __init__(self, config: ClaudeCodeConfig, usage_log_path: Path | None = None) -> None:
        self._config = config
        # claude -p 1回ごとのトークン使用量・コスト（--output-format json の usage 等）。
        # usage_log_path が指定されていれば JSONL として追記もする。
        self._usage_log_path = usage_log_path
        self.usage_records: list[dict] = []

    def generate_test(
        self,
        worktree: Path,
        task: TestGenerationTask,
        feedback: str | None = None,
    ) -> str:
        prompt = _TASK_PROMPT.format(
            test_patterns=", ".join(task.test_patterns),
            test_command=task.test_command,
            issue_title=task.issue_title,
            issue_body=task.issue_body,
            source_diff=task.source_diff,
        )
        is_rust = task.language.lower() == "rust"
        if is_rust:
            prompt += _RUST_BOTTOM_UP_ADDENDUM
        if feedback:
            prompt += _FEEDBACK_SUFFIX.format(feedback=feedback)

        bash_patterns = [f"Bash({_test_command_prefix(task.test_command)} *)"]
        if is_rust:
            # test_command が "cargo test" 以外(例: "cargo nextest run", ラッパー経由等)でも、
            # ボトムアップ構築での `cargo check` による自己検証は常に使えるようにする。
            bash_patterns.append("Bash(cargo *)")
        allowed_tools = f"{self._config.allowed_tools}," + ",".join(bash_patterns)

        cmd = [
            self._config.command, "-p", prompt,
            "--output-format", "json",
            "--permission-prompts", "none",
            "--restricted",
            # --tools は「利用可能なツールの完全なリスト」なので、Bash だけでなく
            # Read/Write/Edit 等も明示しないとファイルを書けなくなる（2.1.317で確認）
            "--tools", f"{self._config.allowed_tools},Bash",
            "--allowedTools", allowed_tools,
        ]

        try:
            result = subprocess.run(
                cmd, cwd=worktree, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self._config.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude -p timed out after {self._config.timeout}s") from e

        data = _parse_json(result.stdout)
        self._record_usage(task, data, result.returncode)
        if data is None:
            raise RuntimeError(
                f"claude -p produced no parseable JSON output "
                f"(exit={result.returncode}): {result.stderr[:2000]}"
            )
        if data.get("is_error"):
            raise RuntimeError(f"claude -p reported an error: {data.get('result', '')[:2000]}")

        denials = data.get("permission_denials") or []
        if denials:
            logger.debug(f"  claude -p: {len(denials)} permission denial(s) (expected for out-of-scope calls)")

        return _extract_model(data)

    def _record_usage(self, task: TestGenerationTask, data: dict | None, returncode: int) -> None:
        data = data or {}
        usage = data.get("usage") or {}
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bug_id": task.bug_id,
            "model": _extract_model(data),
            "exit_code": returncode,
            "is_error": bool(data.get("is_error")) if data else True,
            "num_turns": data.get("num_turns"),
            "duration_ms": data.get("duration_ms"),
            "total_cost_usd": data.get("total_cost_usd"),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0),
            "model_usage": data.get("modelUsage"),
            "permission_denials": data.get("permission_denials") or [],
            "result_excerpt": str(data.get("result", ""))[-2000:],
        }
        self.usage_records.append(entry)
        if self._usage_log_path:
            self._usage_log_path.parent.mkdir(parents=True, exist_ok=True)
            with self._usage_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _test_command_prefix(test_command: str) -> str:
    """test_command の先頭トークンをBash許可パターンの土台にする（近似。詳細はdoc参照）。"""
    parts = test_command.split()
    return parts[0] if parts else test_command


def _parse_json(stdout: str) -> dict | None:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _extract_model(data: dict) -> str:
    model_usage = data.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        return next(iter(model_usage))
    return ""
