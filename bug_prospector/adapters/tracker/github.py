from __future__ import annotations

import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

import requests as _requests
import github as gh
from github import Github, GithubException, RateLimitExceededException

from ...core.models.issue import IssueInfo, UserInfo
from ...core.models.query import IssueQuery
from ...core.models.record import PRInfo
from .base import IssueTrackerAdapter

logger = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"https?://github\.com/([^/]+/[^/]+?)(?:\.git)?/?$")

# GitHub検索APIが1クエリで返せる最大件数
_SEARCH_CAP = 1000
# search.since 未指定時の検索開始日（GitHubのサービス開始より前）
_GITHUB_EPOCH = date(2007, 1, 1)


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
        """
        条件に合う closed Issue を作成日の新しい順に返す。

        query.labels が空ならラベルで絞り込まない（ラベルでバグを管理していないプロジェクト向け。
        バグかどうかは収集時のLLM判定に任せる）。GitHub検索APIは1クエリあたり最大1000件しか
        返さないため、超える場合は作成日の期間を二分割して検索し直す。
        """
        slug = self.parse_slug(query.repo_url)
        label_terms = "".join(f" label:{_quote_label(l)}" for l in query.labels)
        linked_pr_term = " linked:pr" if query.linked_pr else ""
        base_q = f"repo:{slug} is:issue is:closed{label_terms}{linked_pr_term}"

        since = query.since.date() if query.since else _GITHUB_EPOCH
        until = query.until.date() if query.until else datetime.now(timezone.utc).date()

        results: list[IssueInfo] = []
        for issue in self._search_by_date(base_q, since, until, query.limit):
            results.append(self._to_issue_info(issue, slug))
            if query.limit is not None and len(results) >= query.limit:
                break
        return results

    def _search_by_date(
        self, base_q: str, since: date, until: date, limit: int | None, date_field: str = "created",
    ):
        """
        [since, until] の Issue/PR を新しい順に返す。1000件を超える期間は二分割して再帰的に検索する。
        date_field は期間の基準（Issue は "created"、PR は "merged"）。
        """
        q = f"{base_q} {date_field}:{since.isoformat()}..{until.isoformat()}"
        logger.debug(f"GitHub search: {q}")
        paginated = self._gh.search_issues(q, sort="created", order="desc")
        total = self._total_count(paginated)
        if total > _SEARCH_CAP and since < until and (limit is None or limit > _SEARCH_CAP):
            mid = since + (until - since) // 2
            yield from self._search_by_date(base_q, mid + timedelta(days=1), until, limit, date_field)
            yield from self._search_by_date(base_q, since, mid, limit, date_field)
            return
        if total > _SEARCH_CAP:
            logger.warning(f"GitHub search returned {total} results for {q}; only the first {_SEARCH_CAP} are reachable")
        yield from self._paginate(paginated)

    def _total_count(self, paginated) -> int:
        while True:
            try:
                return paginated.totalCount
            except RateLimitExceededException:
                self._wait_for_rate_limit()

    # ------------------------------------------------------------------
    # PR起点・直接コミット収集用（doc/pr-collection.md）
    # ------------------------------------------------------------------

    def search_merged_prs(
        self, repo_url: str, labels: list[str], since: datetime | None, until: datetime | None,
        limit: int | None,
    ):
        """マージ済みPR（検索結果の Issue オブジェクト）をマージ日の新しい順に返すジェネレータ。"""
        slug = self.parse_slug(repo_url)
        label_terms = "".join(f" label:{_quote_label(l)}" for l in labels)
        base_q = f"repo:{slug} is:pr is:merged{label_terms}"
        lo = since.date() if since else _GITHUB_EPOCH
        hi = until.date() if until else datetime.now(timezone.utc).date()
        yield from self._search_by_date(base_q, lo, hi, limit, date_field="merged")

    def get_pull_details(self, repo_slug: str, number: int) -> dict:
        """PR起点の収集に必要なPRの詳細（base・head・マージSHA・コミット件名・作成者種別等）。"""
        pr = self._call(lambda: self._gh.get_repo(repo_slug).get_pull(number))
        commits = self._call(lambda: [c.commit.message.splitlines()[0] if c.commit.message else "" for c in pr.get_commits()])
        return {
            "number": pr.number,
            "url": pr.html_url,
            "title": pr.title or "",
            "body": pr.body or "",
            "labels": [l.name for l in pr.labels],
            "user_login": pr.user.login if pr.user else "",
            "user_type": pr.user.type if pr.user else "",
            "created_at": _utc(pr.created_at),
            "merged_at": _utc(pr.merged_at) if pr.merged_at else None,
            "merged": bool(pr.merged),
            "base_ref": pr.base.ref,
            "head_sha": pr.head.sha or "",
            "merge_commit_sha": pr.merge_commit_sha or "",
            "commit_subjects": commits,
        }

    def get_default_branch(self, repo_slug: str) -> str:
        return self._call(lambda: self._gh.get_repo(repo_slug).default_branch)

    def get_commit_pulls(self, repo_slug: str, sha: str) -> list[int]:
        """コミットを含むマージ済みPRの番号（squash/rebase で取り込まれたPRの判定に使う）。"""
        data = self._rest_get(f"repos/{repo_slug}/commits/{sha}/pulls")
        return [p["number"] for p in (data or []) if p.get("merged_at")]

    def get_ci_status(self, repo_slug: str, sha: str) -> dict:
        """
        コミットのCI結果をまとめる。check-runs（GitHub Actions 等）と旧来の commit status（Travis 等）の両方を見る。
        state: failure（1つでも失敗）/ success（成功のみ）/ pending（実行中のみ）/ none（結果なし）
        """
        failures: list[str] = []
        successes = pending = 0
        runs = self._rest_get(f"repos/{repo_slug}/commits/{sha}/check-runs", {"per_page": 100}) or {}
        for run in runs.get("check_runs", []):
            if run.get("status") != "completed":
                pending += 1
            elif run.get("conclusion") in ("failure", "timed_out", "cancelled", "startup_failure"):
                failures.append(run.get("name", ""))
            elif run.get("conclusion") in ("success", "neutral", "skipped"):
                successes += 1
        status = self._rest_get(f"repos/{repo_slug}/commits/{sha}/status") or {}
        for s in status.get("statuses", []):
            if s.get("state") in ("failure", "error"):
                failures.append(s.get("context", ""))
            elif s.get("state") == "success":
                successes += 1
            elif s.get("state") == "pending":
                pending += 1
        if failures:
            state = "failure"
        elif successes:
            state = "success"
        elif pending:
            state = "pending"
        else:
            state = "none"
        return {"state": state, "failed": failures, "succeeded": successes, "pending": pending}

    def _rest_get(self, path: str, params: dict | None = None):
        """PyGithub が直接サポートしないエンドポイント用。404 は None。レート制限は待ってから再試行する。"""
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        for _ in range(3):
            resp = _requests.get(f"https://api.github.com/{path}", headers=headers, params=params, timeout=30)
            if resp.status_code == 404 or resp.status_code == 422:
                return None
            if resp.status_code in (403, 429) and "rate limit" in resp.text.lower():
                self._wait_for_rate_limit()
                continue
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError(f"GitHub API {path}: rate limited repeatedly")

    def _call(self, fn):
        """PyGithub 呼び出しをレート制限時に待って再試行する。"""
        while True:
            try:
                return fn()
            except RateLimitExceededException:
                self._wait_for_rate_limit()

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

        candidates: list[dict] = []
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
                break

            if resp.status_code != 200:
                logger.debug(f"Timeline API {resp.status_code} for #{issue_number}")
                break

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
                # 別リポジトリ（forkや依存クレート）のPRは、このリポジトリの修正ではない
                pr_repo = (pr_data.get("repository") or {}).get("full_name", repo_slug)
                if pr_repo.lower() != repo_slug.lower():
                    continue
                candidates.append(pr_data)

            if len(items) < 100:
                break
            page += 1

        # Issueに言及しただけのPRより、"Fixes #N" 等で修正を明言しているPRを優先する
        candidates.sort(key=lambda p: not _mentions_fix(p, issue_number))
        for pr_data in candidates:
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
                        head_sha=pr.head.sha or "",
                    )
            except GithubException as e:
                logger.debug(f"get_pull({pr_number}) failed: {e}")

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
        # 途中でレート制限に当たったら、待ってから続き（既に返した件数より後）から再開する
        yielded = 0
        while True:
            try:
                for i, item in enumerate(paginated_list):
                    if i < yielded:
                        continue
                    yielded += 1
                    yield item
                return
            except RateLimitExceededException:
                self._wait_for_rate_limit()

    def _wait_for_rate_limit(self) -> None:
        # PyGithub 2.x の get_rate_limit() は RateLimitOverview を返し、枠ごとの値は .resources 配下
        limits = self._gh.get_rate_limit().resources
        # 検索APIは core とは別枠（30回/分）。使い切った枠のリセットまで待つ
        # （どちらも残っているなら secondary rate limit とみなし1分待つ）
        exhausted = [r.reset for r in (limits.search, limits.core) if r.remaining == 0]
        if exhausted:
            wait = max(0, (min(exhausted) - datetime.now(timezone.utc)).total_seconds()) + 5
        else:
            wait = 60
        logger.warning(f"Rate limit exceeded — waiting {wait:.0f}s ...")
        time.sleep(wait)

    @staticmethod
    def parse_slug(url: str) -> str:
        m = _SLUG_RE.match(url)
        if not m:
            raise ValueError(f"Invalid GitHub URL: {url}")
        return m.group(1)


_FIX_KEYWORDS = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)"


def _mentions_fix(pr_data: dict, issue_number: int) -> bool:
    """PRのタイトル/本文が "Fixes #N" や "Fix for #N"、"Fixes: <issue URL>" 等で修正を明言しているか。"""
    text = f"{pr_data.get('title') or ''}\n{pr_data.get('body') or ''}"
    ref = rf"(?:#|/issues/){issue_number}\b"
    return re.search(rf"\b{_FIX_KEYWORDS}\b[^\n#]{{0,80}}?{ref}", text, re.IGNORECASE) is not None


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
