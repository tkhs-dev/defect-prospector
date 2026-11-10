from __future__ import annotations

import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from bug_prospector.adapters.vcs.git import GitAdapter
from bug_prospector.config import LanguageConfig, LLMConfig, ProjectConfig, SourcesConfig
from bug_prospector.core.collector import Collector, SourceTally, _Run
from bug_prospector.core.models.issue import KIND_COMMIT, KIND_PULL_REQUEST
from bug_prospector.core.models.record import ValidationResult
from bug_prospector.core.sources import (
    MERGE_COMMIT,
    REBASE,
    SQUASH,
    closing_issue_refs,
    first_parent_commits,
    matches_keywords,
    resolve_pr_fix_range,
    source_change_rejection,
)
from bug_prospector.core.storage import Storage


def _git(repo: Path, *args: str, date: str = "2024-01-01T00:00:00Z") -> str:
    env = dict(os.environ, GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env, check=True).stdout.strip()


def _commit(repo: Path, path: str, content: str, msg: str) -> str:
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    (repo / path).write_text(content, encoding="utf-8")
    _git(repo, "add", path)
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "t")
    _git(r, "config", "commit.gpgsign", "false")
    _commit(r, "src/lib.rs", "a\n", "init")
    return r


class TestResolvePrFixRange:
    def test_merge_commit_uses_first_parent(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "src/lib.rs", "b\n", "Fix one")
        _commit(repo, "src/lib.rs", "c\n", "Fix two")
        _git(repo, "checkout", "-q", "main")
        _git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #1", "feature")
        merge = _git(repo, "rev-parse", "HEAD")

        rng = resolve_pr_fix_range(repo, merge, ["Fix one", "Fix two"])
        assert (rng.buggy_sha, rng.merge_method) == (base, MERGE_COMMIT)

    def test_rebase_includes_all_pr_commits(self, repo):
        base = _git(repo, "rev-parse", "HEAD")
        _commit(repo, "src/lib.rs", "b\n", "Fix one")
        last = _commit(repo, "src/lib.rs", "c\n", "Fix two")

        rng = resolve_pr_fix_range(repo, last, ["Fix one", "Fix two"])
        assert (rng.buggy_sha, rng.merge_method) == (base, REBASE)

    def test_squash_uses_parent(self, repo):
        _commit(repo, "src/lib.rs", "b\n", "Unrelated earlier commit")
        parent = _git(repo, "rev-parse", "HEAD")
        squashed = _commit(repo, "src/lib.rs", "c\n", "Fix everything (#2)")

        rng = resolve_pr_fix_range(repo, squashed, ["wip", "fix review comments"])
        assert (rng.buggy_sha, rng.merge_method) == (parent, SQUASH)


class TestFilters:
    def test_keywords(self):
        assert matches_keywords("Fixes panic on empty input", ["fix"]) is True
        assert matches_keywords("Add prefix option", ["fix"]) is False  # 単語の途中は不一致
        assert matches_keywords("anything", None) is True

    def test_closing_refs(self):
        assert closing_issue_refs("Fixes #12 and closes https://github.com/o/r/issues/34") == [12, 34]
        assert closing_issue_refs("Related to #12") == []

    _SRC = "diff --git a/src/lib.rs b/src/lib.rs\n--- a/src/lib.rs\n+++ b/src/lib.rs\n@@ -1 +1 @@\n-a\n+b\n"
    _DOC = "diff --git a/README.md b/README.md\n--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b\n"
    _TEST = "diff --git a/tests/t.rs b/tests/t.rs\n--- a/tests/t.rs\n+++ b/tests/t.rs\n@@ -1 +1 @@\n-a\n+b\n"

    def test_source_change_accepted(self):
        assert source_change_rejection(self._SRC + self._TEST, ["tests/**"], ["*.md"], 100) is None

    def test_tests_only_rejected(self):
        assert "tests only" in source_change_rejection(self._TEST, ["tests/**"], ["*.md"], 100)

    def test_docs_only_rejected(self):
        assert "non-code" in source_change_rejection(self._DOC, ["tests/**"], ["*.md"], 100)

    def test_too_large_rejected(self):
        assert "too large" in source_change_rejection(self._SRC, ["tests/**"], [], 1)


class TestFirstParentCommits:
    def test_lists_direct_commits_newest_first_without_merges(self, repo):
        _git(repo, "checkout", "-q", "-b", "feature")
        _commit(repo, "src/lib.rs", "b\n", "PR commit")
        _git(repo, "checkout", "-q", "main")
        direct = _commit(repo, "src/other.rs", "x\n", "Fix direct\n\nbody text")
        _git(repo, "merge", "-q", "--no-ff", "-m", "Merge pull request #1", "feature")

        commits = first_parent_commits(repo, "main", None, None)
        assert [c["subject"] for c in commits] == ["Fix direct", "init"]
        assert commits[0]["sha"] == direct
        assert commits[0]["body"] == "body text"


# ---------------------------------------------------------------------------
# Collector の PR起点・直接コミット収集（GitHub API は差し替え、git は実リポジトリ）
# ---------------------------------------------------------------------------

class _FakeTracker:
    def __init__(self, details: dict | None = None, commit_pulls: dict | None = None) -> None:
        self.details = details or {}
        self.commit_pulls = commit_pulls or {}

    def get_pull_details(self, slug, number):
        return self.details[number]

    def get_commit_pulls(self, slug, sha):
        return self.commit_pulls.get(sha, [])


class _FakeValidator:
    def validate(self, **kwargs):
        return ValidationResult(status="invalid")


def _run(tmp_path: Path, repo: Path, tracker, sources: SourcesConfig) -> tuple[Collector, _Run]:
    project = ProjectConfig(
        project_id="o-r", repo_url="https://github.com/o/r", tracker_url="https://github.com/o/r",
        language=LanguageConfig(type="rust", test_patterns=["tests/**"]),
        llm=LLMConfig(), sources=sources,
    )
    collector = Collector(Storage(tmp_path / "ds"), tmp_path / "ds")
    run = _Run(
        project=project, tracker=tracker, vcs=GitAdapter(), slug="o/r", flat="o-r",  # type: ignore[arg-type]
        local_repo=repo, validator=_FakeValidator(), dry_run=False, default_branch="main",  # type: ignore[arg-type]
    )
    return collector, run


def _pr_details(number: int, merge_sha: str, subjects: list[str], base: str = "main") -> dict:
    now = datetime(2024, 1, 2, tzinfo=timezone.utc)
    return {
        "number": number, "url": f"https://github.com/o/r/pull/{number}", "title": "Fix f",
        "body": "", "labels": [], "user_login": "dev", "user_type": "User",
        "created_at": now, "merged_at": now, "merged": True, "base_ref": base,
        "head_sha": "h" * 40, "merge_commit_sha": merge_sha, "commit_subjects": subjects,
    }


class TestCollectPullRequests:
    def test_builds_pr_record_with_kind_and_merge_method(self, tmp_path, repo):
        base = _git(repo, "rev-parse", "HEAD")
        merge = _commit(repo, "src/lib.rs", "b\n", "Fix f (#5)")
        collector, run = _run(tmp_path, repo, _FakeTracker({5: _pr_details(5, merge, ["Fix f"])}), SourcesConfig())

        record = collector._build_pr_record(run, "github-o-r-pr-5", 5)

        assert record.source.kind == KIND_PULL_REQUEST
        assert record.repository.buggy_commit == base
        assert record.pr.merge_method == SQUASH
        assert record.pr.head_sha == "h" * 40
        assert record.validation.status == "invalid"

    def test_skips_pr_to_non_default_branch(self, tmp_path, repo):
        merge = _commit(repo, "src/lib.rs", "b\n", "Backport")
        tracker = _FakeTracker({5: _pr_details(5, merge, ["Backport"], base="release-1.x")})
        collector, run = _run(tmp_path, repo, tracker, SourcesConfig())

        assert collector._build_pr_record(run, "github-o-r-pr-5", 5) is None

    def test_skips_docs_only_pr(self, tmp_path, repo):
        merge = _commit(repo, "README.md", "docs\n", "Fix typo")
        collector, run = _run(tmp_path, repo, _FakeTracker({5: _pr_details(5, merge, ["Fix typo"])}), SourcesConfig())

        assert collector._build_pr_record(run, "github-o-r-pr-5", 5) is None

    def test_precheck_skips_pr_already_collected_via_issue(self, tmp_path, repo):
        collector, run = _run(tmp_path, repo, _FakeTracker(), SourcesConfig())
        run.known_prs.add(5)
        item = SimpleNamespace(number=5, title="Fix f", body="", user=SimpleNamespace(login="dev", type="User"))

        assert "already collected" in collector._pr_precheck(run, "github-o-r-pr-5", item)

    def test_precheck_skips_bots_and_keywords(self, tmp_path, repo):
        sources = SourcesConfig()
        sources.pull_requests.title_keywords = ["fix"]
        collector, run = _run(tmp_path, repo, _FakeTracker(), sources)
        bot = SimpleNamespace(number=6, title="Fix deps", body="", user=SimpleNamespace(login="dependabot[bot]", type="Bot"))
        feature = SimpleNamespace(number=7, title="Add option", body="", user=SimpleNamespace(login="dev", type="User"))

        assert "bot" in collector._pr_precheck(run, "github-o-r-pr-6", bot)
        assert "keyword" in collector._pr_precheck(run, "github-o-r-pr-7", feature)


class TestCollectCommits:
    def _commit_dict(self, repo: Path, sha: str) -> dict:
        return next(c for c in first_parent_commits(repo, "main", None, None) if c["sha"] == sha)

    def test_builds_commit_record(self, tmp_path, repo):
        base = _git(repo, "rev-parse", "HEAD")
        sha = _commit(repo, "src/lib.rs", "b\n", "Fix f")
        collector, run = _run(tmp_path, repo, _FakeTracker(), SourcesConfig())

        reason, record = collector._build_commit_record(run, f"github-o-r-commit-{sha[:12]}", self._commit_dict(repo, sha))

        assert reason == ""
        assert record.source.kind == KIND_COMMIT
        assert record.source.issue_url == f"https://github.com/o/r/commit/{sha}"
        assert record.repository.buggy_commit == base
        assert record.pr is None

    def test_skips_commit_belonging_to_pr(self, tmp_path, repo):
        sha = _commit(repo, "src/lib.rs", "b\n", "Fix f (#9)")
        collector, run = _run(tmp_path, repo, _FakeTracker(commit_pulls={sha: [9]}), SourcesConfig())

        reason, record = collector._build_commit_record(run, "id", self._commit_dict(repo, sha))
        assert record is None and "PR #9" in reason

    def test_skips_already_known_fix_commit(self, tmp_path, repo):
        sha = _commit(repo, "src/lib.rs", "b\n", "Fix f")
        collector, run = _run(tmp_path, repo, _FakeTracker(), SourcesConfig())
        run.known_fix_commits.add(sha)

        reason, record = collector._build_commit_record(run, "id", self._commit_dict(repo, sha))
        assert record is None and "already collected" in reason

    def test_tally_counts_per_source(self, tmp_path, repo):
        sha = _commit(repo, "src/lib.rs", "b\n", "Fix f")
        collector, run = _run(tmp_path, repo, _FakeTracker(), SourcesConfig())
        tally = SourceTally()
        _, record = collector._build_commit_record(run, "github-o-r-commit-x", self._commit_dict(repo, sha))
        collector._save(run, tally, record)

        assert (tally.new, tally.validation_counts) == (1, {"invalid": 1})
        assert sha in run.known_fix_commits
        assert collector._storage.exists("github-o-r-commit-x")


class _RecordingChecker:
    def __init__(self) -> None:
        self.ci_shas = None

    def run(self, config, fix_sha, buggy_sha, source_diff, ci_shas=None):
        self.ci_shas = ci_shas
        from bug_prospector.core.models.record import FixCheckResult
        return {"revert": FixCheckResult(passed=False, detail="reverted")}


class TestFinishRecordChecks:
    def test_pr_ci_uses_head_only_and_exclude_rejects(self, tmp_path, repo):
        merge = _commit(repo, "src/lib.rs", "b\n", "Fix f (#5)")
        sources = SourcesConfig()
        sources.pull_requests.fix_checks.revert = "exclude"
        collector, run = _run(tmp_path, repo, _FakeTracker({5: _pr_details(5, merge, ["Fix f"])}), sources)
        run.checker = _RecordingChecker()  # type: ignore[assignment]

        record = collector._build_pr_record(run, "github-o-r-pr-5", 5)

        assert run.checker.ci_shas == ["h" * 40]
        assert record.validation.status == "fix_rejected"
        assert record.fix_checks["revert"].passed is False

    def test_commit_ci_uses_fix_commit_and_record_mode_keeps_validating(self, tmp_path, repo):
        sha = _commit(repo, "src/lib.rs", "b\n", "Fix f")
        sources = SourcesConfig()
        sources.commits.fix_checks.revert = "record"
        collector, run = _run(tmp_path, repo, _FakeTracker(), sources)
        run.checker = _RecordingChecker()  # type: ignore[assignment]
        commit = next(c for c in first_parent_commits(repo, "main", None, None) if c["sha"] == sha)

        _, record = collector._build_commit_record(run, "id", commit)

        assert run.checker.ci_shas == [sha]
        assert record.validation.status == "invalid"
        assert record.fix_checks["revert"].passed is False
