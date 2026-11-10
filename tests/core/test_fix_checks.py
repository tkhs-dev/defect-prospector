from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from bug_prospector.config import FixCheckConfig
from bug_prospector.core.fix_checks import FixChecker, added_lines, rejected_checks
from bug_prospector.core.models.record import FixCheckResult


def _git(repo: Path, *args: str, date: str | None = None) -> str:
    env = dict(os.environ)
    if date:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = date
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env, check=True)
    return r.stdout.strip()


def _commit(repo: Path, path: str, content: str, msg: str, date: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", msg, date=date)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    _git(tmp_path, "config", "commit.gpgsign", "false")
    _commit(tmp_path, "src/lib.rs", "a\nb\nc\n", "init", "2024-01-01T00:00:00Z")
    return tmp_path


def _diff(repo: Path, a: str, b: str) -> str:
    return _git(repo, "diff", a, b)


class TestAddedLines:
    def test_collects_new_side_line_numbers(self):
        diff = (
            "diff --git a/src/x.rs b/src/x.rs\n--- a/src/x.rs\n+++ b/src/x.rs\n"
            "@@ -1,3 +1,4 @@\n a\n-b\n+B\n+B2\n c\n"
            "@@ -10,2 +11,2 @@\n x\n+y\n"
        )
        assert added_lines(diff) == {"src/x.rs": {2, 3, 12}}

    def test_ignores_deleted_files(self):
        diff = "diff --git a/old.rs b/old.rs\n--- a/old.rs\n+++ /dev/null\n@@ -1 +0,0 @@\n-a\n"
        assert added_lines(diff) == {}


class TestRevert:
    def test_detects_revert(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        fix = _commit(repo, "src/lib.rs", "a\nB\nc\n", "Fix b", "2024-02-01T00:00:00Z")
        _git(repo, "revert", "--no-edit", fix, date="2024-02-02T00:00:00Z")

        result = FixChecker(repo, "main").check_revert(fix, base)
        assert result.passed is False
        assert "Revert" in result.detail

    def test_not_reverted(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        fix = _commit(repo, "src/lib.rs", "a\nB\nc\n", "Fix b", "2024-02-01T00:00:00Z")
        _commit(repo, "src/other.rs", "x\n", "Unrelated", "2024-02-02T00:00:00Z")

        assert FixChecker(repo, "main").check_revert(fix, base).passed is True


class TestFollowUpFix:
    def test_detects_lines_changed_within_window(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        fix = _commit(repo, "src/lib.rs", "a\nB\nc\n", "Fix b", "2024-02-01T00:00:00Z")
        follow = _commit(repo, "src/lib.rs", "a\nBB\nc\n", "Fix the fix", "2024-02-10T00:00:00Z")

        result = FixChecker(repo, "main").check_follow_up_fix(fix, _diff(repo, base, fix), 30)
        assert result.passed is False
        assert result.data["follow_up_commits"] == [follow]
        assert "Fix the fix" in result.detail

    def test_change_after_window_is_ignored(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        fix = _commit(repo, "src/lib.rs", "a\nB\nc\n", "Fix b", "2024-02-01T00:00:00Z")
        _commit(repo, "src/lib.rs", "a\nBB\nc\n", "Much later change", "2024-06-01T00:00:00Z")

        assert FixChecker(repo, "main").check_follow_up_fix(fix, _diff(repo, base, fix), 30).passed is True

    def test_change_to_other_lines_is_ignored(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        fix = _commit(repo, "src/lib.rs", "a\nB\nc\n", "Fix b", "2024-02-01T00:00:00Z")
        _commit(repo, "src/lib.rs", "A\nB\nc\n", "Touch line 1", "2024-02-05T00:00:00Z")

        assert FixChecker(repo, "main").check_follow_up_fix(fix, _diff(repo, base, fix), 30).passed is True

    def test_no_later_commits(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        fix = _commit(repo, "src/lib.rs", "a\nB\nc\n", "Fix b", "2024-02-01T00:00:00Z")

        assert FixChecker(repo, "main").check_follow_up_fix(fix, _diff(repo, base, fix), 30).passed is True


class TestRelease:
    def test_first_tag_containing_fix(self, repo):
        fix = _commit(repo, "src/lib.rs", "a\nB\nc\n", "Fix b", "2024-02-01T00:00:00Z")
        _git(repo, "tag", "v1.0.1", date="2024-02-03T00:00:00Z")
        _commit(repo, "src/lib.rs", "a\nB\nC\n", "next", "2024-03-01T00:00:00Z")
        _git(repo, "tag", "v1.0.2", date="2024-03-02T00:00:00Z")

        result = FixChecker(repo, "main").check_release(fix)
        assert result.passed is True
        assert result.data["first_tag"] == "v1.0.1"

    def test_unreleased(self, repo):
        fix = _commit(repo, "src/lib.rs", "a\nB\nc\n", "Fix b", "2024-02-01T00:00:00Z")
        assert FixChecker(repo, "main").check_release(fix).passed is False


class TestCi:
    @pytest.mark.parametrize("states, expected", [
        ({"h": "success", "f": "none"}, True),
        ({"h": "failure", "f": "success"}, False),
        ({"h": "none", "f": "pending"}, None),
    ])
    def test_combines_states(self, repo, states, expected):
        checker = FixChecker(repo, "main", ci_lookup=lambda sha: {"state": states[sha]})
        assert checker.check_ci(["h", "f"]).passed is expected

    def test_without_lookup_is_unknown(self, repo):
        assert FixChecker(repo, "main").check_ci(["f"]).passed is None


class TestRun:
    def test_runs_only_enabled_checks_and_rejects_in_exclude_mode(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        fix = _commit(repo, "src/lib.rs", "a\nB\nc\n", "Fix b", "2024-02-01T00:00:00Z")
        config = FixCheckConfig(revert="record", release="exclude")

        results = FixChecker(repo, "main").run(config, fix, base, _diff(repo, base, fix))

        assert set(results) == {"revert", "release"}
        assert rejected_checks(results, config) == ["release"]

    def test_record_mode_never_rejects(self):
        results = {"revert": FixCheckResult(passed=False, detail="")}
        assert rejected_checks(results, FixCheckConfig(revert="record")) == []

    def test_unknown_result_never_rejects(self):
        results = {"ci": FixCheckResult(passed=None, detail="")}
        assert rejected_checks(results, FixCheckConfig(ci="exclude")) == []

    def test_check_error_is_recorded_as_unknown(self, repo):
        config = FixCheckConfig(release="exclude")
        results = FixChecker(repo, "main").run(config, "0" * 40, "0" * 40, "")
        assert results["release"].passed is None
        assert rejected_checks(results, config) == []


class TestFixCheckConfig:
    def test_per_source_override_merges_over_defaults(self):
        base = FixCheckConfig.from_dict({"revert": "exclude", "ci": True})
        override = FixCheckConfig.from_dict({"ci": False, "release": "record"}, base=base)
        assert (override.revert, override.ci, override.release) == ("exclude", "off", "record")
        assert base.ci == "record"

    def test_rejects_unknown_mode(self):
        with pytest.raises(ValueError):
            FixCheckConfig.from_dict({"revert": "drop"})
