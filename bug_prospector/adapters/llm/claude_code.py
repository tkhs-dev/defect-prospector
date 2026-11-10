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
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from ...config import ClaudeCodeConfig
from ...core.models.record import BUG_CATEGORIES, TESTABILITY_CATEGORIES
from .base import BugVerdict, LLMAdapter, TestabilityVerdict, TestGenerationTask

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


_SCREENING_PROMPT = """You are screening candidate bugs for an automated bug-dataset collection tool. Each candidate is a GitHub issue that was closed by a merged fix. A later step will ask an LLM to write a regression test that FAILS on the code before the fix and PASSES after it. Decide whether such a test can realistically be written for this candidate, so that unsuitable candidates are excluded from the dataset.

Answer testable=true only if ALL of the following hold:
  - The root cause is in the program's own source code (its logic/behaviour),     and the fix below changes that behaviour.
  - The behavioural difference can be observed deterministically by an automated     test using the project's test framework ({language}; test command:     `{test_command}`), without special hardware, a specific OS/terminal, real     network services, manual interaction, or timing luck.

Otherwise answer testable=false. Typical reasons:
  - not_program_bug: documentation/typo, CI or build configuration, packaging,     dependency or toolchain version bumps, refactoring with no behaviour change,     or the actual cause lies outside this project (upstream crate, compiler, user error).
  - environment_dependent: the bug only shows up on a particular platform,     terminal, locale, filesystem, network condition or timing, and cannot be     reproduced reliably in a test.
  - insufficient_information: the issue and diff do not make clear what behaviour     differs before and after the fix.
Use category=program_bug when testable=true.

Judge only from the information below; do not ask questions. Keep the reason to one or two sentences.

## Issue
Title: {issue_title}

{issue_body}

## Fix (source-only diff; any test changes from the original fix, if present, are omitted)
```diff
{source_diff}
```
"""

_SCREENING_SCHEMA = {
    "type": "object",
    "properties": {
        "testable": {"type": "boolean"},
        "category": {"type": "string", "enum": list(TESTABILITY_CATEGORIES)},
        "reason": {"type": "string"},
    },
    "required": ["testable", "category", "reason"],
    "additionalProperties": False,
}

_BUG_CLASSIFICATION_PROMPT = """\
You are filtering GitHub issues for an automated bug-dataset collection tool. \
The issue below was closed, and the change shown below was identified as its \
fix (from a merged pull request or commit that references the issue; this \
link is heuristic and may be wrong).

Decide whether this is a genuine BUG that the change fixes: the issue reports \
behaviour of the project's own code that is incorrect (wrong output, crash/panic, \
spurious error, regression, code that should compile but doesn't because of the \
project, etc.), and the change corrects that behaviour in the project's source.

Answer is_bug=false when any of these apply:
  - feature_request: the issue asks for new functionality or an enhancement \
    to behaviour that was working as designed.
  - question: a usage question or support request, or the reporter's own mistake.
  - documentation: only documentation, comments or examples are wrong or changed.
  - maintenance: build/CI/packaging/dependency/toolchain chores, refactoring \
    or performance work with no incorrect behaviour.
  - unrelated_fix: the issue describes a bug, but the change shown does not \
    actually fix it (the link between issue and change is wrong).
  - other: spam, discussion, or anything else that is not a bug fix.
Use category=bug when is_bug=true.

Judge only from the information below; do not ask questions. Keep the reason to \
one or two sentences.

## Issue
Title: {issue_title}

{issue_body}

## Change identified as the fix
Title: {fix_title}

Source-only diff (any test changes are omitted):
```diff
{source_diff}
```
"""

# PR起点・直接コミット用: 独立したバグ報告（Issue）は無く、修正者自身の説明しかない
_FIX_ONLY_CLASSIFICATION_PROMPT = """\
You are filtering code changes for an automated bug-dataset collection tool. \
The change below was merged into the default branch {origin}. There is NO \
separate bug report: the only description is the one written by the author \
of the change itself, so judge mainly from what the code change actually does.

Decide whether this change is a genuine BUG FIX: before the change, the \
project's own code behaved incorrectly (wrong output, crash/panic, spurious \
error, regression, code that should compile but doesn't because of the \
project, etc.), and the change corrects that behaviour in the project's source.

Answer is_bug=false when any of these apply:
  - feature_request: the change adds new functionality or enhances behaviour \
    that was working as designed.
  - documentation: only documentation, comments or examples are changed.
  - maintenance: build/CI/packaging/dependency/toolchain chores, lint or \
    warning cleanups, refactoring or performance work with no incorrect behaviour.
  - other: anything else that is not a bug fix.
Use category=bug when is_bug=true.

Judge only from the information below; do not ask questions. Keep the reason to \
one or two sentences.

## {description_heading}
Title: {issue_title}

{issue_body}

## Source-only diff (any test changes are omitted)
```diff
{source_diff}
```
"""

_BUG_CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "is_bug": {"type": "boolean"},
        "category": {"type": "string", "enum": list(BUG_CATEGORIES)},
        "reason": {"type": "string"},
    },
    "required": ["is_bug", "category", "reason"],
    "additionalProperties": False,
}


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
        if is_rust and self._config.rust_bottom_up:
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

        data = self._run(cmd, worktree, task, kind="generate")

        denials = data.get("permission_denials") or []
        if denials:
            logger.debug(f"  claude -p: {len(denials)} permission denial(s) (expected for out-of-scope calls)")

        return _extract_model(data)

    def assess_testability(self, task: TestGenerationTask) -> TestabilityVerdict:
        prompt = _SCREENING_PROMPT.format(
            language=task.language,
            test_command=task.test_command,
            issue_title=task.issue_title,
            issue_body=task.issue_body,
            source_diff=task.source_diff,
        )
        verdict, model = self._judge(prompt, _SCREENING_SCHEMA, "testable", task, kind="screen")
        return TestabilityVerdict(
            testable=verdict["testable"],
            category=str(verdict.get("category", "")),
            reason=str(verdict.get("reason", "")),
            model=model,
        )

    def classify_bug(self, task: TestGenerationTask) -> BugVerdict:
        if task.source_kind == "issue":
            prompt = _BUG_CLASSIFICATION_PROMPT.format(
                issue_title=task.issue_title,
                issue_body=task.issue_body,
                fix_title=task.fix_title or "(unknown)",
                source_diff=task.source_diff,
            )
        else:
            is_pr = task.source_kind == "pull_request"
            prompt = _FIX_ONLY_CLASSIFICATION_PROMPT.format(
                origin="through a pull request" if is_pr else "as a direct commit (no pull request)",
                description_heading="Pull request description" if is_pr else "Commit message",
                issue_title=task.issue_title,
                issue_body=task.issue_body or "(empty)",
                source_diff=task.source_diff,
            )
        verdict, model = self._judge(prompt, _BUG_CLASSIFICATION_SCHEMA, "is_bug", task, kind="classify")
        return BugVerdict(
            is_bug=verdict["is_bug"],
            category=str(verdict.get("category", "")),
            reason=str(verdict.get("reason", "")),
            model=model,
        )

    def _judge(
        self, prompt: str, schema: dict, decision_key: str, task: TestGenerationTask, kind: str,
    ) -> tuple[dict, str]:
        """ツールなしの claude -p に構造化出力で判定させ、(判定結果の dict, モデル名) を返す。"""
        # 判定はプロンプト内の情報だけで行わせる（ツールは一切与えない）。
        # cwd に CLAUDE.md 等が無い空ディレクトリを使い、無関係な文脈が混ざらないようにする。
        cmd = [
            self._config.command, "-p", prompt,
            "--output-format", "json",
            "--permission-prompts", "none",
            "--restricted",
            "--tools", "",
            "--json-schema", json.dumps(schema),
        ]
        with tempfile.TemporaryDirectory(prefix=f"bp-{kind}-") as cwd:
            data = self._run(cmd, Path(cwd), task, kind=kind)

        verdict = data.get("structured_output")
        if not isinstance(verdict, dict):
            verdict = _parse_json(str(data.get("result", "")))
        if not isinstance(verdict, dict) or not isinstance(verdict.get(decision_key), bool):
            raise RuntimeError(f"claude -p returned no usable {kind} verdict: {str(data.get('result', ''))[:2000]}")
        return verdict, _extract_model(data)

    def _run(self, cmd: list[str], cwd: Path, task: TestGenerationTask, kind: str) -> dict:
        """claude -p を実行し、使用量を記録したうえで JSON 出力を返す。失敗時は RuntimeError。"""
        try:
            result = subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=self._config.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(f"claude -p timed out after {self._config.timeout}s") from e

        data = _parse_json(result.stdout)
        self._record_usage(task, data, result.returncode, kind)
        if data is None:
            raise RuntimeError(
                f"claude -p produced no parseable JSON output "
                f"(exit={result.returncode}): {result.stderr[:2000]}"
            )
        if data.get("is_error"):
            raise RuntimeError(f"claude -p reported an error: {data.get('result', '')[:2000]}")
        return data

    def _record_usage(self, task: TestGenerationTask, data: dict | None, returncode: int, kind: str) -> None:
        data = data or {}
        usage = data.get("usage") or {}
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "bug_id": task.bug_id,
            "kind": kind,  # "screen"（生成可否の判定） / "generate"（テスト生成）
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
