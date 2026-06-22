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
from pathlib import Path

import git

from ..config import LanguageConfig, ValidationConfig
from .models.record import (
    PATCH_FAILED,
    SETUP_FAILED,
    SKIPPED,
    ERROR,
    TestRunResult,
    ValidationResult,
)
from .patch import apply_reverse_patch, split_diff

logger = logging.getLogger(__name__)

_VALID = "valid"
_INVALID = "invalid"


class Validator:

    def __init__(
        self,
        lang_config: LanguageConfig,
        val_config: ValidationConfig,
        worktree_path: Path | None = None,
        patch_dir: Path | None = None,
        test_log_dir: Path | None = None,
    ) -> None:
        self._lang = lang_config
        self._val = val_config
        self._worktree_path = worktree_path  # 永続worktreeのパス; None=一時worktree
        self._patch_dir = patch_dir           # パッチ保存先; None=保存しない
        self._test_log_dir = test_log_dir     # テスト出力保存先; None=保存しない
        self._active_worktree: Path | None = None
        self._worktree_repo: git.Repo | None = None

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
        use_persistent = self._worktree_path is not None
        worktree: Path
        created_temp: bool = False

        if use_persistent:
            worktree = self._worktree_path  # type: ignore[assignment]
            if self._active_worktree is None:
                # 初回: worktree が既に存在するか確認
                if worktree.exists() and (worktree / ".git").exists():
                    logger.debug(f"Reusing existing worktree at {worktree}")
                else:
                    logger.debug(f"Creating persistent worktree at {worktree} for {fix_ref[:8]}")
                    repo.git.worktree("add", str(worktree), fix_ref)
                self._active_worktree = worktree
                self._worktree_repo = repo
            # fix_ref に切り替え
            logger.debug(f"Switching worktree to {fix_ref[:8]}")
            subprocess.run(
                ["git", "reset", "--hard", fix_ref],
                cwd=worktree, capture_output=True, text=True,
            )
            subprocess.run(
                ["git", "clean", "-fd"],
                cwd=worktree, capture_output=True, text=True,
            )
        else:
            worktree = repo_path.parent / f"{repo_path.name}-validate-{uuid.uuid4().hex[:8]}"
            logger.debug(f"Creating temp worktree at {worktree} for {fix_ref[:8]}")
            repo.git.worktree("add", str(worktree), fix_ref)
            created_temp = True

        try:
            # --- 環境セットアップ ---
            ok, err = self._run_setup(worktree)
            if not ok:
                logger.warning(f"  setup failed: {err[:300]}")
                return ValidationResult(status=SETUP_FAILED)

            # --- fix 状態でテスト ---
            log_stem = bug_id or fix_ref[:16]
            fix_log = self._test_log_dir / f"{log_stem}-fix.log" if self._test_log_dir else None
            fix_result = self._run_tests(worktree, log_path=fix_log)
            if self._val.show_failed_tests:
                fix_result.failed_tests = self._extract_failed_tests(fix_result.stdout, fix_result.stderr)
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
            if self._val.show_failed_tests:
                buggy_result.failed_tests = self._extract_failed_tests(buggy_result.stdout, buggy_result.stderr)
            logger.debug(
                f"  buggy tests: {'PASS' if buggy_result.passed else 'FAIL'} "
                f"({buggy_result.duration_seconds:.1f}s)"
            )
            if self._val.show_failed_tests and buggy_result.failed_tests:
                for name in buggy_result.failed_tests:
                    logger.info(f"    [buggy FAIL] {name}")

            is_valid = fix_result.passed and not buggy_result.passed
            return ValidationResult(
                status=_VALID if is_valid else _INVALID,
                fix_result=fix_result,
                buggy_result=buggy_result,
            )

        except Exception as e:
            logger.exception(f"Validation error: {e}")
            return ValidationResult(status=ERROR)

        finally:
            if use_persistent and self._active_worktree:
                # リバースパッチの変更を元に戻して次回の使い回しに備える
                subprocess.run(
                    ["git", "reset", "--hard", fix_ref],
                    cwd=self._active_worktree, capture_output=True, text=True,
                )
                subprocess.run(
                    ["git", "clean", "-fd"],
                    cwd=self._active_worktree, capture_output=True, text=True,
                )
            elif created_temp:
                try:
                    repo.git.worktree("remove", "--force", str(worktree))
                    logger.debug(f"Removed temp worktree {worktree}")
                except Exception as e:
                    logger.debug(f"Failed to remove temp worktree: {e}")

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _run_setup(self, cwd: Path) -> tuple[bool, str]:
        for cmd in self._lang.setup_commands:
            print(f"  Running setup command: {cmd} at {cwd}")
            try:
                r = subprocess.run(
                    cmd, shell=True, cwd=cwd, errors="replace",
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
                stdout=r.stdout[-4096:],
                stderr=r.stderr[-2048:],
                duration_seconds=duration,
            )
        except subprocess.TimeoutExpired:
            return TestRunResult(
                passed=False,
                exit_code=-1,
                stderr="[test run timed out]",
                duration_seconds=self._lang.test_timeout,
            )

    def _extract_failed_tests(self, stdout: str, stderr: str) -> list[str]:
        """テスト出力から失敗したテストケース名を抽出する。"""
        combined = stdout + "\n" + stderr
        lang = self._lang.type

        if lang == "rust":
            # cargo test: "test path::to::test_name ... FAILED"
            return re.findall(r'^test (\S+) \.\.\. FAILED', combined, re.MULTILINE)

        if lang == "python":
            # pytest: "FAILED tests/foo.py::TestClass::test_name - ..."
            return re.findall(r'^FAILED (\S+)', combined, re.MULTILINE)

        if lang in ("javascript", "typescript"):
            # Jest: "  ✕ test name (Nms)" or "  × test name"
            names = re.findall(r'^\s+[✕×✗]\s+(.+?)(?:\s+\(\d+\s*m?s\))?$', combined, re.MULTILINE)
            return [n.strip() for n in names]

        if lang == "go":
            # go test: "--- FAIL: TestName (0.00s)"
            return re.findall(r'^--- FAIL: (\S+)', combined, re.MULTILINE)

        if lang == "java":
            # JUnit/Maven: "[ERROR] testName  Time elapsed: ... <<< FAILURE!"
            return re.findall(r'^\[ERROR\] (\S+).*<<<\s+FAIL', combined, re.MULTILINE)

        # 汎用フォールバック: FAILED / FAIL: を含む行
        results = []
        for line in combined.splitlines():
            stripped = line.strip()
            if re.search(r'\bFAILED\b|\bFAIL:', stripped):
                results.append(stripped)
        return results