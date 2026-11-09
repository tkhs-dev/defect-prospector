from __future__ import annotations

import pytest

from bug_prospector.adapters.tracker.github import GitHubAdapter, _quote_label


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
