"""
テストの FAIL→PASS を確認してバグの有効性を検証する。

戦略 (reverse_patch_source):
  1. fix commit のスナップショットを worktree に用意（永続 or 一時）
  2. セットアップコマンドで依存関係を導入
  3. fix 状態でテスト実行  → PASS を期待
  4. ソースファイルのみ逆パッチ適用（テストファイルはそのまま保持）
  5. リバース後でテスト実行 → FAIL を期待
  6. PASS かつ NOT PASS → status="valid"（バグ認定）

worktree_path を指定すると単一ディレクトリを使い回す（ディスク節約）。
patch_dir を指定すると適用したパッチを bug_id.patch として保存する。
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import git

from ..adapters.coverage import get_coverage_adapter
from ..adapters.language import get_language_adapter
from ..adapters.llm.base import LLMAdapter, TestGenerationTask
from ..config import CoverageConfig, LanguageConfig, ValidationConfig
from .coverage import CoverageRunner
from .models.record import (
    PATCH_FAILED,
    SETUP_FAILED,
    SKIPPED,
    ERROR,
    GeneratedTestInfo,
    TestRunResult,
    ValidationResult,
)
from .patch import apply_reverse_patch, is_test_file, split_diff

logger = logging.getLogger(__name__)

_VALID = "valid"
_INVALID = "invalid"

# 生成テストファイルからテスト関数/メソッド名を抽出する言語別パターン（ベストエフォート）。
# core/test_effort.py の _TEST_DECL_PATTERNS と同じ発想だが、こちらは名前を捕捉する。
_TEST_NAME_PATTERNS: dict[str, re.Pattern[str]] = {
    "rust": re.compile(r'^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*\(', re.MULTILINE),
    "python": re.compile(r'^\s*(?:async\s+)?def\s+(test_\w+)\s*\(', re.MULTILINE),
    "java": re.compile(r'\bvoid\s+(\w+)\s*\(\s*\)'),
}


@dataclass
class GenerationAttemptResult:
    """LLMによる再現テスト生成の試行結果（doc/test-generation.md 6.3節）。"""
    confirmed: bool
    generated_test: GeneratedTestInfo | None = None
    attempts: int = 0
    last_feedback: str = ""


def _is_valid(fix_result: TestRunResult, buggy_result: TestRunResult) -> bool:
    """バグ修正の有効性を判定する。

    ケース単位のデータがある場合:
      buggy で失敗 かつ fix で成功 に変わったテストが1件以上あれば valid。
      これにより修正とは無関係に元から落ちているテストを除外できる。

    ケースデータがない場合:
      全体結果が FAIL→PASS であれば valid（フォールバック）。
    """
    if fix_result.test_cases and buggy_result.test_cases:
        fix_passed = {tc.name for tc in fix_result.test_cases if tc.passed}
        buggy_failed = {tc.name for tc in buggy_result.test_cases if not tc.passed}
        return bool(fix_passed & buggy_failed)
    return fix_result.passed and not buggy_result.passed


class Validator:

    def __init__(
        self,
        lang_config: LanguageConfig,
        val_config: ValidationConfig,
        worktree_path: Path | None = None,
        patch_dir: Path | None = None,
        test_log_dir: Path | None = None,
        coverage_config: CoverageConfig | None = None,
    ) -> None:
        self._lang = lang_config
        self._val = val_config
        self._worktree_path = worktree_path  # 永続worktreeのパス; None=一時worktree
        self._patch_dir = patch_dir           # パッチ保存先; None=保存しない
        self._test_log_dir = test_log_dir     # テスト出力保存先; None=保存しない
        self._active_worktree: Path | None = None
        self._worktree_repo: git.Repo | None = None
        self._coverage_runner = CoverageRunner(
            adapter=get_coverage_adapter(lang_config.type),
            config=coverage_config or CoverageConfig(),
        )

    def close(self) -> None:
        """永続的な worktree を削除する。run_project の終了時に呼ぶ。"""
        if self._worktree_repo and self._active_worktree:
            try:
                self._worktree_repo.git.worktree(
                    "remove", "--force", str(self._active_worktree)
                )
                logger.debug(f"Removed persistent worktree {self._active_worktree}")
            except Exception as e:
                logger.debug(f"Failed to remove persistent worktree: {e}")
            self._active_worktree = None
            self._worktree_repo = None

    def validate(
        self,
        repo_path: Path,
        fix_ref: str,
        unified_diff: str,
        bug_id: str | None = None,
    ) -> ValidationResult:
        """
        fix_ref のスナップショットに逆パッチを当ててテストを実行し、
        FAIL→PASS が確認できたら status="valid" を返す。
        """
        if not self._val.enabled:
            return ValidationResult(status=SKIPPED)

        repo = git.Repo(repo_path)
        worktree, created_temp = self._acquire_worktree(repo, fix_ref)

        try:
            # --- 環境セットアップ ---
            ok, err = self._run_setup(worktree)
            if not ok:
                logger.warning(f"  setup failed: {err[:300]}")
                return ValidationResult(status=SETUP_FAILED)

            lang_adapter = get_language_adapter(self._lang.type)

            # --- fix 状態でテスト ---
            log_stem = bug_id or fix_ref[:16]
            fix_log = self._test_log_dir / f"{log_stem}-fix.log" if self._test_log_dir else None
            fix_result = self._run_tests(worktree, log_path=fix_log)
            if lang_adapter:
                fix_result.test_cases = lang_adapter.parse_test_output(fix_result.stdout, fix_result.stderr)
            logger.debug(
                f"  fix tests: {'PASS' if fix_result.passed else 'FAIL'} "
                f"({fix_result.duration_seconds:.1f}s)"
            )
            if self._val.show_failed_tests and fix_result.failed_tests:
                for name in fix_result.failed_tests:
                    logger.info(f"    [fix  FAIL] {name}")

            # --- リバースパッチ対象を決定 ---
            if self._val.strategy == "reverse_patch_source":
                source_diff, _ = split_diff(unified_diff, self._lang.test_patterns)
                patch_target = source_diff if source_diff.strip() else unified_diff
            else:
                patch_target = unified_diff

            # --- パッチ保存先 ---
            patch_save_path: Path | None = None
            if self._patch_dir is not None:
                name = bug_id or fix_ref[:16]
                patch_save_path = self._patch_dir / f"{name}.patch"

            # --- リバースパッチ適用 ---
            ok, err = apply_reverse_patch(worktree, patch_target, save_path=patch_save_path)
            if not ok:
                logger.warning(f"  reverse patch failed: {err[:300]}")
                return ValidationResult(status=PATCH_FAILED, fix_result=fix_result)
            if patch_save_path:
                logger.debug(f"  patch saved: {patch_save_path}")

            # --- バグ状態でテスト ---
            buggy_log = self._test_log_dir / f"{log_stem}-buggy.log" if self._test_log_dir else None
            buggy_result = self._run_tests(worktree, log_path=buggy_log)
            if lang_adapter:
                buggy_result.test_cases = lang_adapter.parse_test_output(buggy_result.stdout, buggy_result.stderr)
            logger.debug(
                f"  buggy tests: {'PASS' if buggy_result.passed else 'FAIL'} "
                f"({buggy_result.duration_seconds:.1f}s)"
            )
            if self._val.show_failed_tests and buggy_result.failed_tests:
                for name in buggy_result.failed_tests:
                    logger.info(f"    [buggy FAIL] {name}")

            is_valid = _is_valid(fix_result, buggy_result)

            # --- カバレッジ計測（バグ状態＝逆パッチ適用後のワークツリーで実行） ---
            # 修正差分が触れた行を、既存テストが buggy 時点でどれだけカバーしていたかを見る。
            coverage_result = self._coverage_runner.measure(worktree, unified_diff)

            return ValidationResult(
                status=_VALID if is_valid else _INVALID,
                fix_result=fix_result,
                buggy_result=buggy_result,
                coverage=coverage_result,
            )

        except Exception as e:
            logger.exception(f"Validation error: {e}")
            return ValidationResult(status=ERROR)

        finally:
            self._release_worktree(repo, worktree, fix_ref, created_temp)

    def validate_with_generated_test(
        self,
        repo_path: Path,
        fix_ref: str,
        source_diff: str,
        llm: LLMAdapter,
        task: TestGenerationTask,
        bug_id: str,
        max_attempts: int,
    ) -> GenerationAttemptResult:
        """
        LLMAdapter で再現テストを生成し、fix:PASS / buggy:FAIL を機械的に確認する
        （doc/test-generation.md 6.3節）。confirmed に至るまで最大 max_attempts 回試行する。
        """
        repo = git.Repo(repo_path)
        worktree, created_temp = self._acquire_worktree(repo, fix_ref)
        lang_adapter = get_language_adapter(self._lang.type)
        feedback: str | None = None

        try:
            ok, err = self._run_setup(worktree)
            if not ok:
                logger.warning(f"  [{bug_id}] generated-test setup failed: {err[:300]}")
                return GenerationAttemptResult(confirmed=False, attempts=0, last_feedback=err)

            for attempt in range(1, max_attempts + 1):
                try:
                    model = llm.generate_test(worktree, task, feedback)
                except Exception as e:
                    logger.warning(f"  [{bug_id}] attempt {attempt}: generation failed: {e}")
                    feedback = f"Test generation failed: {e}"
                    self._reset_worktree(worktree, fix_ref)
                    continue

                new_files = _new_test_files(worktree, task.test_patterns)
                if not new_files:
                    logger.debug(f"  [{bug_id}] attempt {attempt}: no new test file matching test_patterns")
                    feedback = (
                        "No new or modified file matching one of these patterns was found: "
                        f"{task.test_patterns}"
                    )
                    self._reset_worktree(worktree, fix_ref)
                    continue

                combined_source = "\n".join(new_files.values())
                candidates = _extract_test_names(combined_source, task.language)

                # --- fix状態: 生成テストがPASSすること ---
                fix_result = self._run_tests(worktree)
                if lang_adapter:
                    fix_result.test_cases = lang_adapter.parse_test_output(fix_result.stdout, fix_result.stderr)
                if not _generated_test_outcome(fix_result, candidates, want_passed=True):
                    logger.debug(f"  [{bug_id}] attempt {attempt}: generated test did not pass at fix state")
                    feedback = (
                        "The generated test did not pass when run against the FIX commit "
                        "(it should pass here, since the bug is already fixed).\n"
                        f"stdout:\n{fix_result.stdout[-2000:]}\nstderr:\n{fix_result.stderr[-2000:]}"
                    )
                    self._reset_worktree(worktree, fix_ref)
                    continue

                # --- buggy状態: ソース差分のみ逆パッチして生成テストがFAILすること ---
                ok, err = apply_reverse_patch(worktree, source_diff)
                if not ok:
                    logger.debug(f"  [{bug_id}] attempt {attempt}: reverse patch failed: {err[:300]}")
                    feedback = f"Reverse-applying the fix's source diff failed:\n{err[:1000]}"
                    self._reset_worktree(worktree, fix_ref)
                    continue

                buggy_result = self._run_tests(worktree)
                if lang_adapter:
                    buggy_result.test_cases = lang_adapter.parse_test_output(buggy_result.stdout, buggy_result.stderr)

                if _generated_test_outcome(buggy_result, candidates, want_passed=False):
                    file_path, source_code = next(iter(new_files.items()))
                    generated = GeneratedTestInfo(
                        file_path=file_path,
                        function_name=candidates[0] if candidates else None,
                        source_code=source_code,
                        framework=task.language,
                        provider=llm.provider,
                        model=model,
                        attempts=attempt,
                        confirmed=True,
                        generated_at=datetime.now(timezone.utc),
                    )
                    logger.info(f"  [{bug_id}] generated test confirmed ({attempt} attempt(s))")
                    return GenerationAttemptResult(confirmed=True, generated_test=generated, attempts=attempt)

                logger.debug(f"  [{bug_id}] attempt {attempt}: generated test did not fail at buggy state")
                feedback = (
                    "The generated test passed against the FIX commit but did NOT fail when "
                    "the fix's source changes were reverted (it should fail here, to "
                    "demonstrate it reproduces the bug).\n"
                    f"stdout:\n{buggy_result.stdout[-2000:]}\nstderr:\n{buggy_result.stderr[-2000:]}"
                )
                self._reset_worktree(worktree, fix_ref)

            return GenerationAttemptResult(confirmed=False, attempts=max_attempts, last_feedback=feedback or "")

        except Exception as e:
            logger.exception(f"  [{bug_id}] generated-test validation error: {e}")
            return GenerationAttemptResult(confirmed=False, last_feedback=str(e))

        finally:
            self._release_worktree(repo, worktree, fix_ref, created_temp)

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _acquire_worktree(self, repo: git.Repo, fix_ref: str) -> tuple[Path, bool]:
        """fix_ref のスナップショットを持つworktreeを用意する。Returns: (worktree, created_temp)。"""
        if self._worktree_path is not None:
            worktree = self._worktree_path
            if self._active_worktree is None:
                if worktree.exists() and (worktree / ".git").exists():
                    logger.debug(f"Reusing existing worktree at {worktree}")
                else:
                    logger.debug(f"Creating persistent worktree at {worktree} for {fix_ref[:8]}")
                    repo.git.worktree("add", str(worktree), fix_ref)
                self._active_worktree = worktree
                self._worktree_repo = repo
            logger.debug(f"Switching worktree to {fix_ref[:8]}")
            self._reset_worktree(worktree, fix_ref)
            return worktree, False

        worktree = Path(repo.working_dir).parent / f"{Path(repo.working_dir).name}-validate-{uuid.uuid4().hex[:8]}"
        logger.debug(f"Creating temp worktree at {worktree} for {fix_ref[:8]}")
        repo.git.worktree("add", str(worktree), fix_ref)
        return worktree, True

    def _reset_worktree(self, worktree: Path, fix_ref: str) -> None:
        """worktree を fix_ref の状態に戻し、未追跡ファイルを削除する。"""
        subprocess.run(
            ["git", "reset", "--hard", fix_ref],
            cwd=worktree, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=worktree, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )

    def _release_worktree(self, repo: git.Repo, worktree: Path, fix_ref: str, created_temp: bool) -> None:
        if created_temp:
            try:
                repo.git.worktree("remove", "--force", str(worktree))
                logger.debug(f"Removed temp worktree {worktree}")
            except Exception as e:
                logger.debug(f"Failed to remove temp worktree: {e}")
        elif self._active_worktree:
            # 次回の使い回しに備えて変更を元に戻す
            self._reset_worktree(self._active_worktree, fix_ref)

    def _run_setup(self, cwd: Path) -> tuple[bool, str]:
        for cmd in self._lang.setup_commands:
            print(f"  Running setup command: {cmd} at {cwd}")
            try:
                r = subprocess.run(
                    cmd, shell=True, cwd=cwd, encoding="utf-8", errors="replace",
                    capture_output=True, text=True, timeout=300,
                )
                print(r.stdout)
                if r.returncode != 0:
                    return False, r.stderr
            except subprocess.TimeoutExpired:
                return False, "setup command timed out"
        return True, ""

    def _run_tests(self, cwd: Path, log_path: Path | None = None) -> TestRunResult:
        start = time.monotonic()
        try:
            r = subprocess.run(
                self._lang.test_command, shell=True, cwd=cwd,
                encoding="utf-8", errors="replace",
                capture_output=True, text=True,
                timeout=self._lang.test_timeout,
            )
            duration = time.monotonic() - start
            if log_path is not None:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                log_path.write_text(
                    f"exit_code: {r.returncode}  duration: {duration:.1f}s\n"
                    f"=== stdout ===\n{r.stdout}\n"
                    f"=== stderr ===\n{r.stderr}",
                    encoding="utf-8",
                )
                logger.debug(f"  test output saved: {log_path}")
            return TestRunResult(
                passed=r.returncode == 0,
                exit_code=r.returncode,
                stdout=r.stdout,
                stderr=r.stderr,
                duration_seconds=duration,
            )
        except subprocess.TimeoutExpired:
            return TestRunResult(
                passed=False,
                exit_code=-1,
                stderr="[test run timed out]",
                duration_seconds=self._lang.test_timeout,
            )


# ---------------------------------------------------------------------------
# 生成テストの検出・結果判定（validate_with_generated_test 用）
# ---------------------------------------------------------------------------

def _new_test_files(worktree: Path, test_patterns: list[str]) -> dict[str, str]:
    """
    直前の reset からの新規/変更ファイルのうち、test_patterns にマッチするものを
    パス→内容の辞書で返す（LLMAdapter.generate_test が worktree に書き込んだファイルの検出）。
    """
    result = subprocess.run(
        # --untracked-files=all: 新規ディレクトリを "?? dir/" に畳まず個々のファイルを列挙させる
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=worktree, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    files: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        status, path = line[:2].strip(), line[3:].strip()
        if status not in ("??", "A", "M", "AM", "MM"):
            continue
        if not is_test_file(path, test_patterns):
            continue
        try:
            files[path] = (worktree / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    return files


def _extract_test_names(source_code: str, language: str) -> list[str]:
    """生成されたテストファイルの内容からテスト関数/メソッド名を抽出する（ベストエフォート）。"""
    pattern = _TEST_NAME_PATTERNS.get(language.lower())
    if pattern is None:
        return []
    return pattern.findall(source_code)


def _generated_test_outcome(result: TestRunResult, candidates: list[str], want_passed: bool) -> bool:
    """
    生成テストが期待どおりの結果（want_passed）だったかを判定する。
    ケース単位の結果があり、candidates のいずれかの名前にマッチするテストが見つかれば
    そのテスト単体の結果で判定する。見つからなければ全体結果にフォールバックする。
    """
    if candidates and result.test_cases:
        matched = {tc.name for tc in result.test_cases if tc.passed == want_passed}
        if any(any(c in name for name in matched) for c in candidates):
            return True
        other = {tc.name for tc in result.test_cases if tc.passed != want_passed}
        if any(any(c in name for name in other) for c in candidates):
            return False
    return result.passed == want_passed
