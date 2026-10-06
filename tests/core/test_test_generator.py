from __future__ import annotations

from datetime import datetime, timezone

import pytest

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
    VALID,
    ValidationResult,
)
from bug_prospector.core.test_generator import is_candidate

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
