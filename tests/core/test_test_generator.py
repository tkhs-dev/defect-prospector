from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from bug_prospector.adapters.llm.base import LLMAdapter, TestabilityVerdict, TestGenerationTask
from bug_prospector.config import LanguageConfig, LLMConfig
from bug_prospector.core.models.environment import EnvironmentInfo
from bug_prospector.core.models.issue import IssueInfo, UserInfo
from bug_prospector.core.models.record import (
    BugRecord,
    FixInfo,
    GeneratedTestInfo,
    INVALID,
    MetaInfo,
    RepoInfo,
    ReproInfo,
    TestabilityInfo,
    VALID,
    ValidationResult,
)
from bug_prospector.core.storage import Storage
from bug_prospector.core.test_generator import TestGenerator, export_generated_tests, is_candidate
from bug_prospector.core.validator import GenerationAttemptResult

TEST_PATTERNS = ["tests/**", "test_*.py", "*_test.py"]

_DIFF_SOURCE_ONLY = """diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,2 +1,2 @@
-def foo(): return 1
+def foo(): return 2
"""

_DIFF_WITH_TEST = """diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,2 +1,2 @@
-def foo(): return 1
+def foo(): return 2
diff --git a/tests/test_foo.py b/tests/test_foo.py
--- a/tests/test_foo.py
+++ b/tests/test_foo.py
@@ -1,2 +1,3 @@
 def test_foo():
     assert foo() == 2
+    assert True
"""


def _record(
    diff: str = _DIFF_SOURCE_ONLY,
    status: str = INVALID,
    buggy_commit: str = "a",
    fix_commit: str = "b",
    generated_test: GeneratedTestInfo | None = None,
) -> BugRecord:
    now = datetime.now(timezone.utc)
    return BugRecord(
        id="bug-1",
        source=IssueInfo(
            tracker_type="github", tracker_url="https://github.com/example/repo",
            issue_id="1", issue_url="https://github.com/example/repo/issues/1",
            title="title", body="body", labels=["bug"],
            reporter=UserInfo(login="someone"), created_at=now,
        ),
        repository=RepoInfo(vcs_type="git", url="https://github.com/example/repo",
                             buggy_commit=buggy_commit, fix_commit=fix_commit),
        reproduction=ReproInfo(environment=EnvironmentInfo(language="python"), generated_test=generated_test),
        fix=FixInfo(commit=fix_commit, message="fix", author_name="someone", committed_at=now, diff=diff),
        metadata=MetaInfo(collected_at=now),
        validation=ValidationResult(status=status) if status else None,
    )


class TestIsCandidate:
    def test_candidate_when_no_test_in_diff_and_invalid(self):
        assert is_candidate(_record(diff=_DIFF_SOURCE_ONLY, status=INVALID), TEST_PATTERNS) is True

    def test_not_candidate_when_diff_already_has_test(self):
        assert is_candidate(_record(diff=_DIFF_WITH_TEST, status=INVALID), TEST_PATTERNS) is False

    def test_candidate_when_diff_has_test_but_invalid_and_include_fixes_with_tests(self):
        r = _record(diff=_DIFF_WITH_TEST, status=INVALID)
        assert is_candidate(r, TEST_PATTERNS, include_fixes_with_tests=True) is True

    def test_not_candidate_when_diff_has_valid_test_even_if_include_fixes_with_tests(self):
        r = _record(diff=_DIFF_WITH_TEST, status=VALID)
        assert is_candidate(r, TEST_PATTERNS, include_fixes_with_tests=True) is False

    def test_not_candidate_when_screened_as_not_testable(self):
        r = _record(diff=_DIFF_SOURCE_ONLY, status=INVALID)
        r.reproduction.testability = _testability(False)
        assert is_candidate(r, TEST_PATTERNS) is False

    def test_candidate_when_screened_as_testable(self):
        r = _record(diff=_DIFF_SOURCE_ONLY, status=INVALID)
        r.reproduction.testability = _testability(True)
        assert is_candidate(r, TEST_PATTERNS) is True

    def test_not_candidate_when_already_valid(self):
        assert is_candidate(_record(diff=_DIFF_SOURCE_ONLY, status=VALID), TEST_PATTERNS) is False

    def test_not_candidate_without_buggy_commit(self):
        assert is_candidate(_record(diff=_DIFF_SOURCE_ONLY, buggy_commit=""), TEST_PATTERNS) is False

    def test_not_candidate_without_fix_commit(self):
        assert is_candidate(_record(diff=_DIFF_SOURCE_ONLY, fix_commit=""), TEST_PATTERNS) is False

    def test_not_candidate_when_already_confirmed_by_llm(self):
        generated = GeneratedTestInfo(
            file_path="tests/test_gen.py", function_name="test_bug", source_code="...",
            framework="python", provider="claude_code", model="claude-opus-5",
            attempts=1, confirmed=True, generated_at=datetime.now(timezone.utc),
        )
        assert is_candidate(_record(diff=_DIFF_SOURCE_ONLY, generated_test=generated), TEST_PATTERNS) is False

    def test_candidate_when_prior_attempt_unconfirmed(self):
        generated = GeneratedTestInfo(
            file_path="tests/test_gen.py", function_name="test_bug", source_code="...",
            framework="python", provider="claude_code", model="claude-opus-5",
            attempts=3, confirmed=False, generated_at=datetime.now(timezone.utc),
        )
        assert is_candidate(_record(diff=_DIFF_SOURCE_ONLY, generated_test=generated), TEST_PATTERNS) is True

    @pytest.mark.parametrize("status", ["invalid", "skipped", "setup_failed", "patch_failed", "timeout", "error"])
    def test_candidate_for_each_retargetable_status(self, status):
        assert is_candidate(_record(diff=_DIFF_SOURCE_ONLY, status=status), TEST_PATTERNS) is True

    def test_not_candidate_when_no_validation_result_but_status_field_none(self):
        # validation=None はフォールバックで "skipped" 扱い → 対象
        r = _record(diff=_DIFF_SOURCE_ONLY)
        r.validation = None
        assert is_candidate(r, TEST_PATTERNS) is True


def _generated(file_path: str = "tests/test_repro.py") -> GeneratedTestInfo:
    return GeneratedTestInfo(
        file_path=file_path, function_name="test_repro", source_code="def test_repro():\n    assert True\n",
        framework="python", provider="claude_code", model="claude-opus-5-5",
        attempts=2, confirmed=True, generated_at=datetime.now(timezone.utc),
    )


class TestExportGeneratedTests:
    def test_writes_test_file_and_meta(self, tmp_path):
        record = _record(generated_test=_generated())
        exported = export_generated_tests([record], tmp_path)

        test_path = tmp_path / "bug-1" / "tests" / "test_repro.py"
        assert exported == [(record, test_path)]
        assert test_path.read_text(encoding="utf-8") == "def test_repro():\n    assert True\n"
        meta = json.loads((tmp_path / "bug-1" / "meta.json").read_text(encoding="utf-8"))
        assert meta["bug_id"] == "bug-1"
        assert meta["fix_commit"] == "b"
        assert meta["attempts"] == 2
        assert meta["model"] == "claude-opus-5-5"

    def test_skips_records_without_generated_test(self, tmp_path):
        assert export_generated_tests([_record()], tmp_path) == []
        assert not any(tmp_path.iterdir())

    @pytest.mark.parametrize("file_path", ["../../evil.py", "/abs/evil.py", r"C:\abs\evil.py"])
    def test_path_cannot_escape_bug_dir(self, tmp_path, file_path):
        record = _record(generated_test=_generated(file_path))
        [(_, path)] = export_generated_tests([record], tmp_path)
        assert path.resolve().is_relative_to((tmp_path / "bug-1").resolve())
        assert path.name == "evil.py"


def _testability(testable: bool) -> TestabilityInfo:
    return TestabilityInfo(
        testable=testable, category="program_bug" if testable else "not_program_bug", reason="r",
        provider="fake", model="m", assessed_at=datetime.now(timezone.utc),
    )


class _FakeLLM(LLMAdapter):
    provider = "fake"

    def __init__(self, verdict: TestabilityVerdict | Exception) -> None:
        self._verdict = verdict
        self.screened: list[str] = []

    def generate_test(self, worktree: Path, task: TestGenerationTask, feedback: str | None = None) -> str:
        raise AssertionError("TestGenerator calls generate_test only via the validator")

    def assess_testability(self, task: TestGenerationTask) -> TestabilityVerdict:
        self.screened.append(task.bug_id)
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return self._verdict


class _FakeValidator:
    def __init__(self) -> None:
        self.generated_for: list[str] = []

    def validate_with_generated_test(self, *, bug_id: str, **kwargs) -> GenerationAttemptResult:
        self.generated_for.append(bug_id)
        return GenerationAttemptResult(confirmed=False, attempts=1, last_feedback="nope")


def _generator(tmp_path, llm: _FakeLLM, screening: bool = True):
    storage = Storage(tmp_path / "dataset")
    validator = _FakeValidator()
    generator = TestGenerator(
        storage=storage, validator=validator, llm=llm,  # type: ignore[arg-type]
        llm_config=LLMConfig(enabled=True, screening=screening),
        lang_config=LanguageConfig(type="python", test_patterns=TEST_PATTERNS),
    )
    return generator, storage, validator


class TestScreening:
    def test_not_testable_record_is_excluded_and_verdict_saved(self, tmp_path):
        llm = _FakeLLM(TestabilityVerdict(testable=False, category="not_program_bug", reason="docs", model="m"))
        generator, storage, validator = _generator(tmp_path, llm)

        confirmed, failed, skipped, excluded = generator.run(tmp_path, [_record()])

        assert (confirmed, failed, skipped, excluded) == (0, 0, 0, 1)
        assert validator.generated_for == []
        saved = storage.list_all()[0]
        assert saved.reproduction.testability.testable is False
        assert saved.reproduction.testability.category == "not_program_bug"
        assert saved.reproduction.testability.provider == "fake"
        # 保存された判定により、以降の実行では候補にならない
        assert is_candidate(saved, TEST_PATTERNS) is False

    def test_testable_record_proceeds_to_generation(self, tmp_path):
        llm = _FakeLLM(TestabilityVerdict(testable=True, category="program_bug", reason="logic"))
        generator, storage, validator = _generator(tmp_path, llm)

        confirmed, failed, _, excluded = generator.run(tmp_path, [_record()])

        assert (confirmed, failed, excluded) == (0, 1, 0)
        assert validator.generated_for == ["bug-1"]
        assert storage.list_all()[0].reproduction.testability.testable is True

    def test_already_screened_record_is_not_asked_again(self, tmp_path):
        llm = _FakeLLM(TestabilityVerdict(testable=False, category="not_program_bug", reason=""))
        generator, _, validator = _generator(tmp_path, llm)
        record = _record()
        record.reproduction.testability = _testability(True)

        generator.run(tmp_path, [record])

        assert llm.screened == []
        assert validator.generated_for == ["bug-1"]

    def test_screening_error_skips_generation_without_saving(self, tmp_path):
        llm = _FakeLLM(RuntimeError("claude -p timed out"))
        generator, storage, validator = _generator(tmp_path, llm)

        confirmed, failed, _, excluded = generator.run(tmp_path, [_record()])

        assert (confirmed, failed, excluded) == (0, 1, 0)
        assert validator.generated_for == []
        assert storage.list_all() == []

    def test_screen_only_does_not_generate(self, tmp_path):
        llm = _FakeLLM(TestabilityVerdict(testable=True, category="program_bug", reason=""))
        generator, storage, validator = _generator(tmp_path, llm)

        generator.run(tmp_path, [_record()], screen_only=True)

        assert llm.screened == ["bug-1"]
        assert validator.generated_for == []
        assert storage.list_all()[0].reproduction.testability.testable is True

    def test_screening_disabled_skips_llm_question(self, tmp_path):
        llm = _FakeLLM(TestabilityVerdict(testable=False, category="not_program_bug", reason=""))
        generator, _, validator = _generator(tmp_path, llm, screening=False)

        generator.run(tmp_path, [_record()])

        assert llm.screened == []
        assert validator.generated_for == ["bug-1"]
