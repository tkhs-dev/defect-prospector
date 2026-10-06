from __future__ import annotations

import subprocess

import git
import pytest

from bug_prospector.core.models.record import TestCaseResult, TestRunResult
from bug_prospector.core.validator import (
    _extract_test_names,
    _generated_test_outcome,
    _new_test_files,
)

TEST_PATTERNS = ["tests/**", "test_*.py", "*_test.py"]


class TestExtractTestNames:
    def test_python(self):
        src = "def test_reproduces_bug():\n    assert foo() == 2\n"
        assert _extract_test_names(src, "python") == ["test_reproduces_bug"]

    def test_rust(self):
        src = '#[test]\nfn reproduces_bug() {\n    assert_eq!(foo(), 2);\n}\n'
        assert _extract_test_names(src, "rust") == ["reproduces_bug"]

    def test_java(self):
        src = "@Test\npublic void reproducesBug() {\n    assertEquals(2, foo());\n}\n"
        assert _extract_test_names(src, "java") == ["reproducesBug"]

    def test_unknown_language_returns_empty(self):
        assert _extract_test_names("def test_x(): pass", "cobol") == []

    def test_no_match_returns_empty(self):
        assert _extract_test_names("x = 1\n", "python") == []


class TestGeneratedTestOutcome:
    def test_case_level_match_passed(self):
        result = TestRunResult(
            passed=False, exit_code=1,
            test_cases=[
                TestCaseResult(name="tests/test_gen.py::test_reproduces_bug", passed=True),
                TestCaseResult(name="tests/test_other.py::test_unrelated", passed=False),
            ],
        )
        assert _generated_test_outcome(result, ["test_reproduces_bug"], want_passed=True) is True

    def test_case_level_match_failed_when_expecting_pass(self):
        result = TestRunResult(
            passed=False, exit_code=1,
            test_cases=[TestCaseResult(name="tests/test_gen.py::test_reproduces_bug", passed=False)],
        )
        assert _generated_test_outcome(result, ["test_reproduces_bug"], want_passed=True) is False

    def test_fallback_to_overall_when_no_case_match(self):
        result = TestRunResult(passed=True, exit_code=0, test_cases=[])
        assert _generated_test_outcome(result, ["test_reproduces_bug"], want_passed=True) is True
        assert _generated_test_outcome(result, [], want_passed=True) is True

    def test_fallback_when_candidate_name_not_found_among_cases(self):
        result = TestRunResult(
            passed=False, exit_code=1,
            test_cases=[TestCaseResult(name="tests/test_other.py::test_unrelated", passed=True)],
        )
        # 生成テスト名がケース一覧に見当たらない → 全体結果にフォールバック
        assert _generated_test_outcome(result, ["test_reproduces_bug"], want_passed=True) is False


class TestNewTestFiles:
    @pytest.fixture
    def repo(self, tmp_path):
        r = git.Repo.init(tmp_path)
        with r.config_writer() as cw:
            cw.set_value("user", "name", "test")
            cw.set_value("user", "email", "test@example.com")
        (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
        r.index.add(["README.md"])
        r.index.commit("initial")
        return tmp_path

    def test_detects_new_untracked_test_file(self, repo):
        (repo / "tests").mkdir()
        (repo / "tests" / "test_new.py").write_text("def test_x(): pass\n", encoding="utf-8")

        files = _new_test_files(repo, TEST_PATTERNS)

        assert files == {"tests/test_new.py": "def test_x(): pass\n"}

    def test_ignores_non_test_files(self, repo):
        (repo / "src").mkdir()
        (repo / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")

        assert _new_test_files(repo, TEST_PATTERNS) == {}

    def test_empty_when_nothing_changed(self, repo):
        assert _new_test_files(repo, TEST_PATTERNS) == {}
