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


class ClaudeCodeAdapter(LLMAdapter):

    provider = "claude_code"

    def __init__(self, config: ClaudeCodeConfig) -> None:
        self._config = config

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
        if feedback:
            prompt += _FEEDBACK_SUFFIX.format(feedback=feedback)

        bash_pattern = f"Bash({_test_command_prefix(task.test_command)} *)"
        allowed_tools = f"{self._config.allowed_tools},{bash_pattern}"

        cmd = [
            self._config.command, "-p", prompt,
            "--output-format", "json",
            "--permission-prompts", "none",
            "--restricted",
            "--tools", "Bash",
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
