"""
テストが同梱されていないバグ修正に対し、LLMで再現テストを合成する（doc/test-generation.md）。

対象: fix.diff にテスト変更が含まれず（split_diff の test_diff が空）、
FAIL→PASS を確認できていないレコード（llm.include_fixes_with_tests が true なら
テスト変更の有無は問わない）。llm.screening が true なら、生成の前にLLMへ
「テストで再現可能か」を尋ね、不可能と答えたレコードは生成せず対象から外す（判定結果は
reproduction.testability に保存し、以降の実行でも対象外のままにする）。bp collect とは別のオプトインコマンド
（bp generate-tests）として実行する。
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from ..adapters.llm.base import LLMAdapter, TestGenerationTask
from ..config import LanguageConfig, LLMConfig
from .models.record import BugRecord, LLM_VALID, TestabilityInfo, ValidationResult
from .patch import split_diff
from .storage import Storage
from .validator import Validator

logger = logging.getLogger(__name__)

# 再挑戦の対象とする既存の validation.status（FAIL→PASS を確認できていない状態）
_RETARGETABLE_STATUSES = {"invalid", "skipped", "setup_failed", "patch_failed", "timeout", "error"}


def is_candidate(record: BugRecord, test_patterns: list[str], include_fixes_with_tests: bool = False) -> bool:
    """
    record が再現テスト生成の対象かどうかを判定する（doc/test-generation.md 3節）。

    include_fixes_with_tests=True のときは、fixにテストが同梱されていても
    FAIL→PASS を確認できていない（有効なテストが付属していない）レコードを対象に含める。
    """
    if not record.repository.buggy_commit or not record.repository.fix_commit:
        return False
    if record.reproduction.generated_test and record.reproduction.generated_test.confirmed:
        return False
    if record.reproduction.testability and not record.reproduction.testability.testable:
        return False
    status = record.validation.status if record.validation else "skipped"
    if status not in _RETARGETABLE_STATUSES:
        return False
    if include_fixes_with_tests:
        return True
    _, test_diff = split_diff(record.fix.diff, test_patterns)
    return not test_diff.strip()


class TestGenerator:
    """候補レコードの列挙・LLM呼び出し（Validator経由）・結果保存を行う薄いオーケストレーター。"""

    def __init__(
        self,
        storage: Storage,
        validator: Validator,
        llm: LLMAdapter,
        llm_config: LLMConfig,
        lang_config: LanguageConfig,
    ) -> None:
        self._storage = storage
        self._validator = validator
        self._llm = llm
        self._llm_config = llm_config
        self._lang = lang_config

    def run(
        self, repo_path: Path, records: list[BugRecord], screen_only: bool = False,
    ) -> tuple[int, int, int, int]:
        """
        screen_only=True のときは生成可否の判定だけ行い、テスト生成はしない。

        Returns: (confirmed, failed, skipped, excluded)
          excluded: 生成可否の判定で「テストで再現不可能」とされ、生成対象から外した件数
        """
        confirmed = failed = skipped = excluded = 0
        self.outcomes: dict[str, tuple[bool, int]] = {}
        for record in records:
            if not is_candidate(record, self._lang.test_patterns, self._llm_config.include_fixes_with_tests):
                skipped += 1
                continue

            task = self._build_task(record)
            if self._llm_config.screening and record.reproduction.testability is None:
                if not self._screen(record, task):
                    # 判定自体に失敗したレコードは、生成せずに次回実行で再判定する
                    failed += 1
                    continue
                if not record.reproduction.testability.testable:
                    excluded += 1
                    continue
            if screen_only:
                continue

            source_diff, _ = split_diff(record.fix.diff, self._lang.test_patterns)

            result = self._validator.validate_with_generated_test(
                repo_path=repo_path,
                fix_ref=record.fix.commit,
                source_diff=source_diff,
                llm=self._llm,
                task=task,
                bug_id=record.id,
                max_attempts=self._llm_config.max_attempts,
            )

            self.outcomes[record.id] = (result.confirmed, result.attempts)
            if result.confirmed:
                record.reproduction.generated_test = result.generated_test
                record.validation = (
                    replace(record.validation, status=LLM_VALID)
                    if record.validation else ValidationResult(status=LLM_VALID)
                )
                self._storage.save(record)
                confirmed += 1
                logger.info(f"  [{record.id}] confirmed ({result.attempts} attempt(s))")
            else:
                failed += 1
                logger.info(f"  [{record.id}] failed after {result.attempts} attempt(s), skipping")

        return confirmed, failed, skipped, excluded

    def _screen(self, record: BugRecord, task: TestGenerationTask) -> bool:
        """LLMに生成可否を判定させ、結果を record に保存する。判定に失敗したら False。"""
        try:
            verdict = self._llm.assess_testability(task)
        except Exception as e:
            logger.warning(f"  [{record.id}] testability screening failed: {e}")
            return False
        record.reproduction.testability = TestabilityInfo(
            testable=verdict.testable,
            category=verdict.category,
            reason=verdict.reason,
            provider=self._llm.provider,
            model=verdict.model,
            assessed_at=datetime.now(timezone.utc),
        )
        self._storage.save(record)
        label = "testable" if verdict.testable else f"NOT testable ({verdict.category})"
        logger.info(f"  [{record.id}] screening: {label}: {verdict.reason}")
        return True

    def _build_task(self, record: BugRecord) -> TestGenerationTask:
        source_diff, _ = split_diff(record.fix.diff, self._lang.test_patterns)
        limit = self._llm_config.max_context_chars
        return TestGenerationTask(
            issue_title=record.source.title,
            issue_body=_truncate(record.source.body, limit),
            source_diff=_truncate(source_diff, limit),
            test_patterns=self._lang.test_patterns,
            language=self._lang.type,
            test_command=self._lang.test_command,
            bug_id=record.id,
        )


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def export_generated_tests(records: list[BugRecord], out_dir: Path) -> list[tuple[BugRecord, Path]]:
    """
    reproduction.generated_test を持つレコードのテストソースをファイルとして書き出す（bp export-tests）。

    出力: <out_dir>/<bug_id>/<generated_test.file_path> と、同じディレクトリの meta.json。
    Returns: 書き出した (レコード, テストファイルのパス) のリスト。
    """
    exported: list[tuple[BugRecord, Path]] = []
    for record in records:
        g = record.reproduction.generated_test
        if g is None:
            continue
        bug_dir = out_dir / record.id
        test_path = bug_dir / _safe_relative_path(g.file_path)
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_text(g.source_code, encoding="utf-8")

        meta = {
            "bug_id": record.id,
            "issue_url": record.source.issue_url,
            "title": record.source.title,
            "buggy_commit": record.repository.buggy_commit,
            "fix_commit": record.repository.fix_commit,
            "validation_status": record.validation.status if record.validation else None,
            "file_path": g.file_path,
            "function_name": g.function_name,
            "framework": g.framework,
            "provider": g.provider,
            "model": g.model,
            "attempts": g.attempts,
            "confirmed": g.confirmed,
            "generated_at": g.generated_at.isoformat(),
        }
        (bug_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        exported.append((record, test_path))
    return exported


def _safe_relative_path(file_path: str) -> Path:
    """LLMが決めたパスが出力先ディレクトリの外を指さないよう、絶対パス・".."を取り除く。"""
    parts = [p for p in PurePosixPath(file_path.replace("\\", "/")).parts if p not in ("", ".", "..", "/")]
    parts = [p for p in parts if not p.endswith(":")]  # Windowsのドライブ指定（"C:"）
    return Path(*parts) if parts else Path("generated_test")
