"""
テストの FAIL→PASS を確認してバグの有効性を検証する。

戦略 (reverse_patch_source):
  1. fix commit の git worktree を作成
  2. セットアップコマンドで依存関係を導入
  3. fix 状態でテスト実行  → PASS を期待
  4. ソースファイルのみ逆パッチ適用（テストファイルはそのまま保持）
  5. リバース後でテスト実行 → FAIL を期待
  6. PASS かつ NOT PASS → status="valid"（バグ認定）
"""
from __future__ import annotations

import logging
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

    def __init__(self, lang_config: LanguageConfig, val_config: ValidationConfig) -> None:
        self._lang = lang_config
        self._val = val_config

    def validate(
        self,
        repo_path: Path,
        fix_ref: str,
        unified_diff: str,
    ) -> ValidationResult:
        """
        fix_ref のスナップショットに逆パッチを当ててテストを実行し、
        FAIL→PASS が確認できたら status="valid" を返す。
        """
        if not self._val.enabled:
            return ValidationResult(status=SKIPPED)

        # worktree は主クローンの隣に作成（git objects を共有するため高速）
        worktree = repo_path.parent / f"{repo_path.name}-validate-{uuid.uuid4().hex[:8]}"
        repo = git.Repo(repo_path)

        try:
            logger.debug(f"Creating worktree at {worktree} for {fix_ref[:8]}")
            repo.git.worktree("add", str(worktree), fix_ref)

            # --- 環境セットアップ ---
            ok, err = self._run_setup(worktree)
            if not ok:
                logger.warning(f"  setup failed: {err[:300]}")
                return ValidationResult(status=SETUP_FAILED)

            # --- fix 状態でテスト ---
            fix_result = self._run_tests(worktree)
            logger.debug(
                f"  fix tests: {'PASS' if fix_result.passed else 'FAIL'} "
                f"({fix_result.duration_seconds:.1f}s)"
            )

            # --- リバースパッチ適用 ---
            if self._val.strategy == "reverse_patch_source":
                source_diff, _ = split_diff(unified_diff, self._lang.test_patterns)
                # ソース変更がゼロ（テストのみ追加）の場合はフルパッチで代替
                patch_target = source_diff if source_diff.strip() else unified_diff
            else:
                patch_target = unified_diff

            ok, err = apply_reverse_patch(worktree, patch_target)
            if not ok:
                logger.warning(f"  reverse patch failed: {err[:300]}")
                return ValidationResult(status=PATCH_FAILED, fix_result=fix_result)

            # --- バグ状態でテスト ---
            buggy_result = self._run_tests(worktree)
            logger.debug(
                f"  buggy tests: {'PASS' if buggy_result.passed else 'FAIL'} "
                f"({buggy_result.duration_seconds:.1f}s)"
            )

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
            try:
                repo.git.worktree("remove", "--force", str(worktree))
                logger.debug(f"Removed worktree {worktree}")
            except Exception as e:
                logger.debug(f"Failed to remove worktree: {e}")

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

    def _run_setup(self, cwd: Path) -> tuple[bool, str]:
        for cmd in self._lang.setup_commands:
            try:
                r = subprocess.run(
                    cmd, shell=True, cwd=cwd,
                    capture_output=True, text=True, timeout=300,
                )
                if r.returncode != 0:
                    return False, r.stderr
            except subprocess.TimeoutExpired:
                return False, "setup command timed out"
        return True, ""

    def _run_tests(self, cwd: Path) -> TestRunResult:
        start = time.monotonic()
        try:
            r = subprocess.run(
                self._lang.test_command, shell=True, cwd=cwd,
                capture_output=True, text=True,
                timeout=self._lang.test_timeout,
            )
            return TestRunResult(
                passed=r.returncode == 0,
                exit_code=r.returncode,
                stdout=r.stdout[-4096:],
                stderr=r.stderr[-2048:],
                duration_seconds=time.monotonic() - start,
            )
        except subprocess.TimeoutExpired:
            return TestRunResult(
                passed=False,
                exit_code=-1,
                stderr="[test run timed out]",
                duration_seconds=self._lang.test_timeout,
            )
