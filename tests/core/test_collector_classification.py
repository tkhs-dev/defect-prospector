from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bug_prospector.adapters.llm.base import BugVerdict, LLMAdapter, TestGenerationTask
from bug_prospector.config import LanguageConfig, LLMConfig, ProjectConfig
from bug_prospector.core.collector import Collector
from bug_prospector.core.models.issue import IssueInfo, UserInfo
from bug_prospector.core.models.record import NOT_BUG, PRInfo, ValidationResult
from bug_prospector.core.storage import Storage

_DIFF = """diff --git a/src/lib.rs b/src/lib.rs
--- a/src/lib.rs
+++ b/src/lib.rs
@@ -1 +1 @@
-fn f() -> u8 { 1 }
+fn f() -> u8 { 2 }
diff --git a/tests/t.rs b/tests/t.rs
--- a/tests/t.rs
+++ b/tests/t.rs
@@ -1 +1,2 @@
 #[test] fn t() {}
+#[test] fn u() {}
"""


class _FakeLLM(LLMAdapter):
    provider = "fake"

    def __init__(self, verdict: BugVerdict | Exception) -> None:
        self._verdict = verdict
        self.tasks: list[TestGenerationTask] = []

    def generate_test(self, worktree: Path, task: TestGenerationTask, feedback: str | None = None) -> str:
        raise AssertionError("not used")

    def classify_bug(self, task: TestGenerationTask) -> BugVerdict:
        self.tasks.append(task)
        if isinstance(self._verdict, Exception):
            raise self._verdict
        return self._verdict


class _FakeVCS:
    def find_buggy_commit(self, repo, fix_sha):
        return "buggy"

    def get_diff(self, repo, buggy, fix):
        return SimpleNamespace(unified_diff=_DIFF, changed_files=["src/lib.rs", "tests/t.rs"], lines_added=2, lines_deleted=1)

    def get_commit_info(self, repo, sha):
        return SimpleNamespace(message="Fix f\n\nlonger body", author_name="a", committed_at=datetime.now(timezone.utc))

    def detect_language(self, repo):
        return "rust"


class _FakeTracker:
    def __init__(self, pr: PRInfo) -> None:
        self._pr = pr

    def get_linked_pr(self, issue_number, slug):
        return self._pr


class _FakeValidator:
    def __init__(self) -> None:
        self.calls = 0

    def validate(self, **kwargs):
        self.calls += 1
        return ValidationResult(status="valid")


def _issue() -> IssueInfo:
    return IssueInfo(
        tracker_type="github", tracker_url="https://github.com/o/r",
        issue_id="7", issue_url="https://github.com/o/r/issues/7",
        title="f returns 1", body="should be 2", labels=[],
        reporter=UserInfo(login="u"), created_at=datetime.now(timezone.utc),
    )


def _project(classify: bool = True) -> ProjectConfig:
    return ProjectConfig(
        project_id="o-r", repo_url="https://github.com/o/r", tracker_url="https://github.com/o/r",
        language=LanguageConfig(type="rust", test_patterns=["tests/**"], test_command="cargo test"),
        llm=LLMConfig(classify_bugs=classify),
    )


def _build(tmp_path, llm: _FakeLLM | None, pr: PRInfo | None = None):
    pr = pr or PRInfo(pr_number=3, pr_url="u", title="Fix f returning 1", merge_commit="fix")
    validator = _FakeValidator()
    collector = Collector(Storage(tmp_path), tmp_path)
    record = collector._build_record(
        bug_id="github-o-r-7", issue=_issue(), project=_project(), slug="o/r",
        local_repo=tmp_path, tracker=_FakeTracker(pr), vcs=_FakeVCS(),  # type: ignore[arg-type]
        validator=validator, dry_run=False, classifier=llm,  # type: ignore[arg-type]
    )
    return record, validator


class TestCollectorBugClassification:
    def test_non_bug_skips_validation_and_is_marked_not_bug(self, tmp_path):
        llm = _FakeLLM(BugVerdict(is_bug=False, category="feature_request", reason="asks for X", model="m"))
        record, validator = _build(tmp_path, llm)

        assert validator.calls == 0
        assert record.validation.status == NOT_BUG
        assert record.bug_classification.is_bug is False
        assert record.bug_classification.category == "feature_request"
        assert record.bug_classification.provider == "fake"

    def test_bug_is_validated_as_usual(self, tmp_path):
        llm = _FakeLLM(BugVerdict(is_bug=True, category="bug", reason="wrong value"))
        record, validator = _build(tmp_path, llm)

        assert validator.calls == 1
        assert record.validation.status == "valid"
        assert record.bug_classification.is_bug is True

    def test_task_has_source_only_diff_and_pr_title(self, tmp_path):
        llm = _FakeLLM(BugVerdict(is_bug=True, category="bug", reason=""))
        _build(tmp_path, llm)

        [task] = llm.tasks
        assert "src/lib.rs" in task.source_diff
        assert "tests/t.rs" not in task.source_diff
        assert task.fix_title == "Fix f returning 1"
        assert task.issue_title == "f returns 1"

    def test_commit_closed_issue_uses_commit_subject_as_fix_title(self, tmp_path):
        llm = _FakeLLM(BugVerdict(is_bug=True, category="bug", reason=""))
        pr = PRInfo(pr_number=0, pr_url="", title="(closed by commit)", merge_commit="fix")
        _build(tmp_path, llm, pr)

        assert llm.tasks[0].fix_title == "Fix f"

    def test_classification_error_raises_so_record_is_not_saved(self, tmp_path):
        llm = _FakeLLM(RuntimeError("claude -p timed out"))
        with pytest.raises(RuntimeError, match="bug classification failed"):
            _build(tmp_path, llm)

    def test_without_classifier_record_has_no_classification(self, tmp_path):
        record, validator = _build(tmp_path, None)

        assert validator.calls == 1
        assert record.bug_classification is None

    def test_classification_round_trips_through_storage(self, tmp_path):
        llm = _FakeLLM(BugVerdict(is_bug=False, category="question", reason="usage question"))
        record, _ = _build(tmp_path, llm)
        storage = Storage(tmp_path / "ds")
        storage.save(record)

        [loaded] = storage.list_all()
        assert loaded.bug_classification == record.bug_classification
        assert loaded.validation.status == NOT_BUG
