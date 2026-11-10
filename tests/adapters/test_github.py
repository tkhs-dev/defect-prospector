from __future__ import annotations

import pytest

import re
from datetime import date, datetime, timedelta, timezone

from bug_prospector.adapters.tracker.github import GitHubAdapter, _mentions_fix, _quote_label
from bug_prospector.core.models.query import IssueQuery


class _FakeResults(list):
    def __init__(self, items, total):
        super().__init__(items)
        self.totalCount = total


class _FakeGithub:
    """1日1件の Issue が days 日分ある想定で、検索API(最大1000件)を模倣する。"""

    def __init__(self, start: date, days: int) -> None:
        self.issues = [start + timedelta(days=i) for i in range(days)]
        self.queries: list[str] = []

    def search_issues(self, q, sort=None, order=None):
        self.queries.append(q)
        lo, hi = re.search(r"created:(\S+)\.\.(\S+)", q).groups()
        hits = sorted((d for d in self.issues if date.fromisoformat(lo) <= d <= date.fromisoformat(hi)), reverse=True)
        return _FakeResults(hits[:1000], len(hits))


def _adapter(fake: _FakeGithub) -> GitHubAdapter:
    adapter = GitHubAdapter(token="")
    adapter._gh = fake  # type: ignore[assignment]
    adapter._to_issue_info = lambda issue, slug: issue  # type: ignore[method-assign]
    return adapter


class TestSearchBugs:
    def test_splits_by_date_beyond_search_cap(self):
        fake = _FakeGithub(date(2016, 1, 1), 2500)
        query = IssueQuery(
            repo_url="https://github.com/o/r", labels=[],
            since=datetime(2016, 1, 1, tzinfo=timezone.utc),
            until=datetime(2016, 1, 1, tzinfo=timezone.utc) + timedelta(days=2499),
        )
        results = _adapter(fake).search_bugs(query)

        assert len(results) == 2500
        assert len(set(results)) == 2500
        assert results == sorted(results, reverse=True)  # 新しい順

    def test_no_label_terms_when_labels_empty(self):
        fake = _FakeGithub(date(2020, 1, 1), 3)
        _adapter(fake).search_bugs(IssueQuery(repo_url="https://github.com/o/r", labels=[]))
        assert "label:" not in fake.queries[0]

    def test_label_terms_when_labels_given(self):
        fake = _FakeGithub(date(2020, 1, 1), 3)
        _adapter(fake).search_bugs(IssueQuery(repo_url="https://github.com/o/r", labels=["C-bug"]))
        assert "label:C-bug" in fake.queries[0]

    def test_limit_stops_early_without_splitting(self):
        fake = _FakeGithub(date(2016, 1, 1), 2500)
        query = IssueQuery(
            repo_url="https://github.com/o/r", labels=[], limit=10,
            since=datetime(2016, 1, 1, tzinfo=timezone.utc),
            until=datetime(2016, 1, 1, tzinfo=timezone.utc) + timedelta(days=2499),
        )
        results = _adapter(fake).search_bugs(query)
        assert len(results) == 10
        assert len(fake.queries) == 1


class TestMentionsFix:
    @pytest.mark.parametrize("title, body", [
        ("Fix panic deserializing flattened any", "Fixes #1379."),
        ("Fixed #1379, flattened struct fields", ""),
        ("Bug fix", "Bug fix for #1379\n\nModified serialize_struct_as_struct."),
        ("bump quote", "Fixes: https://github.com/serde-rs/serde/issues/1379"),
        ("x", "closes #1379"),
    ])
    def test_detects_fix_keyword(self, title, body):
        assert _mentions_fix({"title": title, "body": body}, 1379) is True

    @pytest.mark.parametrize("title, body", [
        ("Produce error message for flatten", "Related to #1379."),
        ("Fix something else", "See #1379 for context"),
        ("x", "Fixes #13790"),
        ("x", None),
    ])
    def test_ignores_mere_mentions(self, title, body):
        assert _mentions_fix({"title": title, "body": body}, 1379) is False


class TestQuoteLabel:
    def test_no_space_unquoted(self):
        assert _quote_label("C-bug") == "C-bug"

    def test_space_quoted(self):
        assert _quote_label("type: bug :bug:") == '"type: bug :bug:"'

    def test_embedded_quote_escaped(self):
        assert _quote_label('foo "bar" baz') == '"foo \\"bar\\" baz"'


class TestParseSlug:
    def test_basic(self):
        assert GitHubAdapter.parse_slug("https://github.com/owner/repo") == "owner/repo"

    def test_trailing_slash(self):
        assert GitHubAdapter.parse_slug("https://github.com/owner/repo/") == "owner/repo"

    def test_dot_git_suffix(self):
        assert GitHubAdapter.parse_slug("https://github.com/owner/repo.git") == "owner/repo"

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError):
            GitHubAdapter.parse_slug("not-a-url")
