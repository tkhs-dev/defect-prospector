from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone

import requests as _requests
import github as gh
from github import Github, GithubException, RateLimitExceededException

from ...core.models.issue import IssueInfo, UserInfo
from ...core.models.query import IssueQuery
from ...core.models.record import PRInfo
from .base import IssueTrackerAdapter

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$")


class GitHubAdapter(IssueTrackerAdapter):

    def __init__(self, token: str | None = None) -> None:
        self._token = token or ""
        self._gh = Github(self._token, per_page=100) if self._token else Github(per_page=100)

    @property
    def tracker_type(self) -> str:
        return "github"

    # ------------------------------------------------------------------
    # Issue 検索
    # ------------------------------------------------------------------

    def search_bugs(self, query: IssueQuery) -> list[IssueInfo]:
        slug = self.parse_slug(query.repo_url)
        label_terms = " ".join(f"label:{_quote_label(l)}" for l in (query.labels or ["bug"]))
        since_term = f" created:>={query.since.strftime('%Y-%m-%d')}" if query.since else ""
        until_term = f" created:<={query.until.strftime('%Y-%m-%d')}" if query.until else ""
        linked_pr_term = " linked:pr" if query.linked_pr else ""

        q = f"repo:{slug} is:issue is:closed {label_terms}{since_term}{until_term}{linked_pr_term}"
        logger.debug(f"GitHub search: {q}")

        results: list[IssueInfo] = []
        for i, issue in enumerate(self._paginate(self._gh.search_issues(q))):
            if query.limit is not None and i >= query.limit:
                break
            results.append(self._to_issue_info(issue, slug))
        return results

    # ------------------------------------------------------------------
    # PR 特定（旧 get_fix_commit の上位互換）
    # ------------------------------------------------------------------

    def get_linked_pr(self, issue_number: int, repo_slug: str) -> PRInfo | None:
        """
        Issue に紐付いたマージ済み PR を返す。

        探索順:
          1. Timeline API の cross-referenced イベント（最も正確）
          2. Issue events の closed イベント（PRマージ時にセットされる commit_id）
        """
        # --- Method 1: Timeline API ---
        pr = self._find_pr_via_timeline(issue_number, repo_slug)
        if pr:
            return pr

        # --- Method 2: closed event の commit_id ---
        try:
            repo = self._gh.get_repo(repo_slug)
            issue = repo.get_issue(issue_number)
            for event in self._paginate(issue.get_events()):
                if event.event == "closed" and event.commit_id:
                    return PRInfo(
                        pr_number=0,
                        pr_url="",
                        title="(closed by commit)",
                        merge_commit=event.commit_id,
                    )
        except GithubException as e:
            logger.debug(f"get_events failed for #{issue_number}: {e}")

        return None

    def get_fix_commit(self, issue_number: int, repo_slug: str) -> str | None:
        """後方互換用ラッパー。"""
        pr = self.get_linked_pr(issue_number, repo_slug)
        return pr.merge_commit if pr else None

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _find_pr_via_timeline(self, issue_number: int, repo_slug: str) -> PRInfo | None:
        """GitHub Timeline API で cross-referenced なマージ済みPRを探す。"""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2026-03-10",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        page = 1
        while True:
            try:
                resp = _requests.get(
                    f"https://api.github.com/repos/{repo_slug}/issues/{issue_number}/timeline",
                    headers=headers,
                    params={"per_page": 100, "page": page},
                    timeout=30,
                )
            except Exception as e:
                logger.debug(f"Timeline request failed: {e}")
                return None

            if resp.status_code != 200:
                logger.debug(f"Timeline API {resp.status_code} for #{issue_number}")
                return None

            items = resp.json()
            for item in items:
                if item.get("event") != "cross-referenced":
                    continue
                source = item.get("source", {})
                if source.get("type") != "issue":
                    continue
                pr_data = source.get("issue", {})
                if pr_data.get("pull_request") is None:
                    continue
                if not pr_data.get("pull_request", {}).get("merged_at"):
                    continue
                pr_number = pr_data["number"]
                try:
                    repo = self._gh.get_repo(repo_slug)
                    pr = repo.get_pull(pr_number)
                    if pr.merged and pr.merge_commit_sha:
                        return PRInfo(
                            pr_number=pr.number,
                            pr_url=pr.html_url,
                            title=pr.title or "",
                            merge_commit=pr.merge_commit_sha,
                        )
                except GithubException as e:
                    logger.debug(f"get_pull({pr_number}) failed: {e}")

            if len(items) < 100:
                break
            page += 1

        return None

    def _to_issue_info(self, issue: gh.Issue.Issue, repo_slug: str) -> IssueInfo:
        return IssueInfo(
            tracker_type="github",
            tracker_url=f"https://github.com/{repo_slug}",
            issue_id=str(issue.number),
            issue_url=issue.html_url,
            title=issue.title or "",
            body=issue.body or "",
            labels=[lbl.name for lbl in issue.labels],
            reporter=_user(issue.user),
            created_at=_utc(issue.created_at),
            closed_at=_utc(issue.closed_at) if issue.closed_at else None,
            assignees=[_user(a) for a in (issue.assignees or [])],
        )

    def _paginate(self, paginated_list):
        """RateLimitExceededException を自動リトライしながらイテレートする。"""
        while True:
            try:
                yield from paginated_list
                return
            except RateLimitExceededException:
                reset_at = self._gh.get_rate_limit().core.reset
                wait = max(0, (reset_at - datetime.now(timezone.utc)).total_seconds()) + 5
                logger.warning(f"Rate limit exceeded — waiting {wait:.0f}s ...")
                time.sleep(wait)

    @staticmethod
    def parse_slug(url: str) -> str:
        m = _SLUG_RE.match(url)
        if not m:
            raise ValueError(f"Invalid GitHub URL: {url}")
        return m.group(1)


def _quote_label(label: str) -> str:
    """GitHub Issue検索の label: 修飾子はスペースを含む値をダブルクォートで囲む必要がある。"""
    if re.search(r'\s', label):
        escaped = label.replace('"', '\\"')
        return f'"{escaped}"'
    return label


def _user(u) -> UserInfo:
    if u is None:
        return UserInfo(login="unknown")
    return UserInfo(login=u.login, url=getattr(u, "html_url", None))


def _utc(dt: datetime) -> datetime:
    if dt is None:
        return datetime.now(timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
