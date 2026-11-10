"""
収集オーケストレーター。

収集元（sources:、doc/pr-collection.md）ごとに修正を特定し、共通の後段処理にかける:

  Issue起点（sources.issues）
    1. GitHub で Issue 検索
    2. 各 Issue に紐付いたマージ済み PR を取得（Timeline API 優先）
    3. PR の merge_commit を fix_commit、その parent を buggy_commit とする
  PR起点（sources.pull_requests）
    1. マージ済みPRを検索（Issue起点で収集済みのPR・既存Issueを修正するPRは除く）
    2. マージ方式（マージコミット / squash / rebase）に応じて buggy/fix を決める
  直接コミット（sources.commits）
    1. デフォルトブランチの first-parent 上の非マージコミットのうち、PRに属さないもの
    2. コミット自身を fix、その parent を buggy とする

  共通の後段処理（_finish_record）
    a. diff 取得
    b. 修正の機械的な確認（fix_checks、doc/fix-checks.md）。exclude で落ちたら fix_rejected
    c. llm.classify_bugs の場合: LLMに「バグの修正か」を判定させ、違えば not_bug
       （doc/bug-classification.md）
    d. validation.enabled の場合: リバースパッチ戦略でテスト検証
    e. BugRecord を Storage に保存（二重取得はスキップ）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..adapters.llm import LLMAdapter, TestGenerationTask, get_llm_adapter
from ..adapters.tracker.github import GitHubAdapter
from ..adapters.vcs.git import GitAdapter
from ..config import SOURCE_NAMES, FixCheckConfig, ProjectConfig
from .fix_checks import FixChecker, rejected_checks
from .models.commit import DiffInfo
from .models.environment import EnvironmentInfo
from .models.issue import KIND_COMMIT, KIND_PULL_REQUEST, IssueInfo, UserInfo
from .models.query import IssueQuery
from .models.record import (
    BugClassificationInfo,
    BugRecord,
    FixInfo,
    MetaInfo,
    PRInfo,
    RepoInfo,
    ReproInfo,
    ValidationResult,
    FIX_REJECTED,
    NOT_BUG,
    SKIPPED,
)
from .patch import split_diff
from .sources import (
    closing_issue_refs,
    first_parent_commits,
    is_bot,
    matches_keywords,
    resolve_pr_fix_range,
    source_change_rejection,
)
from .storage import Storage
from .validator import Validator

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"


class CollectionInterrupted(Exception):
    """Ctrl+C による中断時に中途カウントを運ぶ例外。"""
    def __init__(self, new: int, skipped: int, failed: int, validation_counts: dict[str, int]) -> None:
        self.new = new
        self.skipped = skipped
        self.failed = failed
        self.validation_counts = validation_counts


@dataclass
class SourceTally:
    """収集元ごとの件数。"""
    new: int = 0
    skipped: int = 0
    failed: int = 0
    validation_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class _Run:
    """1回の run_project で共有する状態。"""
    project: ProjectConfig
    tracker: GitHubAdapter
    vcs: GitAdapter
    slug: str
    flat: str
    local_repo: Path
    validator: Validator
    dry_run: bool
    classifier: LLMAdapter | None = None
    checker: FixChecker | None = None
    default_branch: str = ""
    # 収集済み（このトラッカーの既存レコード＋今回保存したもの）の fix コミットとPR番号
    known_fix_commits: set[str] = field(default_factory=set)
    known_prs: set[int] = field(default_factory=set)


class Collector:

    def __init__(self, storage: Storage, dataset_dir: Path, llm: LLMAdapter | None = None) -> None:
        self._storage = storage
        self._dataset_dir = dataset_dir
        # llm.classify_bugs 用。None なら run_project でプロジェクト設定から作る（テストで差し替え可能）
        self._llm = llm
        self.classifier: LLMAdapter | None = None
        self.source_counts: dict[str, SourceTally] = {}

    def run_project(
        self,
        project: ProjectConfig,
        limit_override: int | None = None,
        dry_run: bool = False,
    ) -> tuple[int, int, int, dict[str, int]]:
        """
        ProjectConfig に従って、有効な収集元（Issue → PR → 直接コミットの順）から収集する。
        Returns: (new, skipped, failed, validation_counts)。全収集元の合計。
        収集元ごとの内訳は self.source_counts に入る。
        """
        tracker = GitHubAdapter(token=project.tracker_auth.token)
        vcs = GitAdapter()

        slug = GitHubAdapter.parse_slug(project.tracker_url)
        flat = slug.replace("/", "-")
        local_repo = self._dataset_dir / "repos" / flat

        val = project.validation
        worktree_path = self._dataset_dir / "worktrees" / flat if val.enabled else None
        patch_dir = self._dataset_dir / "patches" / project.project_id if val.enabled else None
        test_log_dir = self._dataset_dir / "test-logs" / project.project_id if val.enabled and val.save_test_output else None
        validator = Validator(
            project.language, val,
            worktree_path=worktree_path, patch_dir=patch_dir, test_log_dir=test_log_dir,
        )

        if not dry_run:
            vcs.ensure_repo(project.repo_url, local_repo)

        run = _Run(
            project=project, tracker=tracker, vcs=vcs, slug=slug, flat=flat,
            local_repo=local_repo, validator=validator, dry_run=dry_run,
        )

        # dry-run ではdiffを取得しないので判定もしない（LLM呼び出しのコストもかけない）
        if project.llm.classify_bugs and not dry_run:
            run.classifier = self._llm or get_llm_adapter(
                project.llm,
                usage_log_path=self._dataset_dir / "llm-usage" / f"{project.project_id}.jsonl",
            )
        self.classifier = run.classifier

        enabled = [n for n in SOURCE_NAMES if getattr(project.sources, n).enabled]
        needs_branch = any(n != "issues" for n in enabled) or any(
            getattr(project.sources, n).fix_checks.any_enabled() for n in enabled
        )
        if needs_branch:
            run.default_branch = tracker.get_default_branch(slug)
            if not dry_run:
                run.checker = FixChecker(
                    local_repo, f"origin/{run.default_branch}",
                    ci_lookup=lambda sha: tracker.get_ci_status(slug, sha),
                )

        for r in self._storage.list_all():
            if r.source.tracker_url.rstrip("/") != project.tracker_url.rstrip("/"):
                continue
            if r.fix.commit:
                run.known_fix_commits.add(r.fix.commit)
            if r.pr and r.pr.pr_number:
                run.known_prs.add(r.pr.pr_number)

        self.source_counts = {}
        collectors = {
            "issues": self._collect_issues,
            "pull_requests": self._collect_pull_requests,
            "commits": self._collect_commits,
        }
        try:
            for name in enabled:
                tally = self.source_counts.setdefault(name, SourceTally())
                logger.info(f"--- source: {name} ---")
                collectors[name](run, tally, limit_override)
        except KeyboardInterrupt:
            total = self._total()
            raise CollectionInterrupted(total.new, total.skipped, total.failed, total.validation_counts)
        finally:
            validator.close()

        total = self._total()
        return total.new, total.skipped, total.failed, total.validation_counts

    def _total(self) -> SourceTally:
        total = SourceTally()
        for t in self.source_counts.values():
            total.new += t.new
            total.skipped += t.skipped
            total.failed += t.failed
            for s, n in t.validation_counts.items():
                total.validation_counts[s] = total.validation_counts.get(s, 0) + n
        return total

    def _save(self, run: _Run, tally: SourceTally, record: BugRecord) -> None:
        if not run.dry_run:
            self._storage.save(record)
        if record.fix.commit:
            run.known_fix_commits.add(record.fix.commit)
        if record.pr and record.pr.pr_number:
            run.known_prs.add(record.pr.pr_number)
        tally.new += 1
        status = record.validation.status if record.validation else "no-validation"
        tally.validation_counts[status] = tally.validation_counts.get(status, 0) + 1
        logger.info(f"  [{record.id}] OK  validation={status}")

    # ------------------------------------------------------------------
    # Issue起点
    # ------------------------------------------------------------------

    def _collect_issues(self, run: _Run, tally: SourceTally, limit_override: int | None) -> None:
        project = run.project
        search = project.search
        query = IssueQuery(
            repo_url=project.tracker_url,
            labels=search.labels,
            state=search.state,
            # require_pr でも検索APIの linked:pr では絞り込まない。linked:pr は GitHub 上で正式に
            # リンクされたPRしか拾わず、古いPR（"Fixes #N" でも未リンク）やコミット直pushで
            # 閉じたIssueを取りこぼすため（serdeでは119件中19件しか残らなかった）。
            # PRの有無は _build_record の get_linked_pr（timeline の cross-reference 等）で判定する。
            linked_pr=None,
            limit=limit_override if limit_override is not None else search.limit,
            since=_parse_dt(search.since),
            until=_parse_dt(search.until),
        )
        issues = run.tracker.search_bugs(query)
        logger.info(f"Found {len(issues)} issues")

        for issue in issues:
            bug_id = f"github-{run.flat}-{issue.issue_id}"
            if self._storage.exists(bug_id):
                tally.skipped += 1
                logger.debug(f"  [{bug_id}] already collected, skipping")
                continue
            try:
                record = self._build_record(
                    bug_id=bug_id, issue=issue, project=project, slug=run.slug,
                    local_repo=run.local_repo, tracker=run.tracker, vcs=run.vcs,
                    validator=run.validator, dry_run=run.dry_run,
                    classifier=run.classifier, checker=run.checker,
                )
            except Exception as e:
                tally.failed += 1
                logger.warning(f"  [{bug_id}] failed: {e}")
                continue
            if record is None:
                # require_pr かつ PR なし → スキップ（カウントは failed ではなく skipped）
                tally.skipped += 1
                continue
            self._save(run, tally, record)

    def _build_record(
        self,
        bug_id: str,
        issue,
        project: ProjectConfig,
        slug: str,
        local_repo: Path,
        tracker: GitHubAdapter,
        vcs: GitAdapter,
        validator: Validator,
        dry_run: bool,
        classifier: LLMAdapter | None = None,
        checker: FixChecker | None = None,
    ) -> BugRecord | None:

        # --- PR 特定 ---
        pr_info: PRInfo | None = tracker.get_linked_pr(int(issue.issue_id), slug)

        if pr_info is None:
            if project.search.require_pr:
                logger.debug(f"  [{bug_id}] no linked PR found, skipping")
                return None
            # PR なしでも続行（fix commit なし）
            return self._build_record_no_pr(bug_id, issue, project, dry_run)

        fix_sha = pr_info.merge_commit
        if dry_run:
            return self._dry_record(bug_id, issue, project, fix_sha, pr_info)

        buggy_sha = vcs.find_buggy_commit(local_repo, fix_sha)
        diff = vcs.get_diff(local_repo, buggy_sha, fix_sha)
        return self._finish_record(
            bug_id=bug_id, source=issue, pr_info=pr_info, fix_sha=fix_sha, buggy_sha=buggy_sha,
            diff=diff, project=project, local_repo=local_repo, vcs=vcs, validator=validator,
            classifier=classifier, checker=checker, checks=project.sources.issues.fix_checks,
        )

    # ------------------------------------------------------------------
    # PR起点
    # ------------------------------------------------------------------

    def _collect_pull_requests(self, run: _Run, tally: SourceTally, limit_override: int | None) -> None:
        cfg = run.project.sources.pull_requests
        search = run.project.search
        limit = limit_override if limit_override is not None else cfg.limit
        items = run.tracker.search_merged_prs(
            run.project.tracker_url, cfg.labels, _parse_dt(search.since), _parse_dt(search.until), limit,
        )
        for i, item in enumerate(items):
            if limit is not None and i >= limit:
                break
            n = item.number
            bug_id = f"github-{run.flat}-pr-{n}"
            reason = self._pr_precheck(run, bug_id, item)
            if reason:
                tally.skipped += 1
                logger.debug(f"  [{bug_id}] skip: {reason}")
                continue
            try:
                record = self._build_pr_record(run, bug_id, n)
            except Exception as e:
                tally.failed += 1
                logger.warning(f"  [{bug_id}] failed: {e}")
                continue
            if record is None:
                tally.skipped += 1
                continue
            self._save(run, tally, record)

    def _pr_precheck(self, run: _Run, bug_id: str, item) -> str | None:
        """検索結果だけで判定できる除外理由（APIの追加呼び出し無し）。"""
        cfg = run.project.sources.pull_requests
        if self._storage.exists(bug_id):
            return "already collected"
        if item.number in run.known_prs:
            return "already collected as the fix of an issue"
        if not matches_keywords(item.title or "", cfg.title_keywords):
            return "title has no keyword"
        user = item.user
        if cfg.exclude_bots and user and is_bot(user.login or "", getattr(user, "type", "") or ""):
            return f"bot author ({user.login})"
        for ref in closing_issue_refs(f"{item.title}\n{item.body or ''}"):
            if self._storage.exists(f"github-{run.flat}-{ref}"):
                return f"fixes issue #{ref}, which is collected from the issue side"
        return None

    def _build_pr_record(self, run: _Run, bug_id: str, number: int) -> BugRecord | None:
        cfg = run.project.sources.pull_requests
        d = run.tracker.get_pull_details(run.slug, number)
        if not d["merged"] or not d["merge_commit_sha"]:
            logger.debug(f"  [{bug_id}] skip: not merged")
            return None
        if d["base_ref"] != run.default_branch:
            logger.debug(f"  [{bug_id}] skip: base is {d['base_ref']}, not {run.default_branch}")
            return None
        merge_sha = d["merge_commit_sha"]
        if merge_sha in run.known_fix_commits:
            logger.debug(f"  [{bug_id}] skip: fix commit already collected")
            return None

        source = IssueInfo(
            tracker_type="github",
            tracker_url=f"https://github.com/{run.slug}",
            issue_id=str(number),
            issue_url=d["url"],
            title=d["title"],
            body=d["body"],
            labels=d["labels"],
            reporter=UserInfo(login=d["user_login"]),
            created_at=d["created_at"],
            closed_at=d["merged_at"],
            kind=KIND_PULL_REQUEST,
        )
        pr_info = PRInfo(
            pr_number=number, pr_url=d["url"], title=d["title"],
            merge_commit=merge_sha, head_sha=d["head_sha"],
        )
        if run.dry_run:
            return self._dry_record(bug_id, source, run.project, merge_sha, pr_info)

        rng = resolve_pr_fix_range(run.local_repo, merge_sha, d["commit_subjects"])
        pr_info.merge_method = rng.merge_method
        diff = run.vcs.get_diff(run.local_repo, rng.buggy_sha, merge_sha)
        reason = source_change_rejection(
            diff.unified_diff, run.project.language.test_patterns, cfg.ignore_paths, cfg.max_source_lines,
        )
        if reason:
            logger.debug(f"  [{bug_id}] skip: {reason}")
            return None
        return self._finish_record(
            bug_id=bug_id, source=source, pr_info=pr_info, fix_sha=merge_sha, buggy_sha=rng.buggy_sha,
            diff=diff, project=run.project, local_repo=run.local_repo, vcs=run.vcs, validator=run.validator,
            classifier=run.classifier, checker=run.checker, checks=cfg.fix_checks,
        )

    # ------------------------------------------------------------------
    # 直接コミット
    # ------------------------------------------------------------------

    def _collect_commits(self, run: _Run, tally: SourceTally, limit_override: int | None) -> None:
        cfg = run.project.sources.commits
        if not (run.local_repo / ".git").exists():
            logger.warning("Direct-commit collection needs a local clone; run without --dry-run once first.")
            return
        search = run.project.search
        limit = limit_override if limit_override is not None else cfg.limit
        commits = first_parent_commits(
            run.local_repo, f"origin/{run.default_branch}", search.since, search.until,
        )
        logger.info(f"Found {len(commits)} first-parent non-merge commits")
        for i, c in enumerate(commits):
            if limit is not None and i >= limit:
                break
            sha = c["sha"]
            bug_id = f"github-{run.flat}-commit-{sha[:12]}"
            try:
                reason, record = self._build_commit_record(run, bug_id, c)
            except Exception as e:
                tally.failed += 1
                logger.warning(f"  [{bug_id}] failed: {e}")
                continue
            if record is None:
                tally.skipped += 1
                logger.debug(f"  [{bug_id}] skip: {reason}")
                continue
            self._save(run, tally, record)

    def _build_commit_record(self, run: _Run, bug_id: str, c: dict) -> tuple[str, BugRecord | None]:
        cfg = run.project.sources.commits
        sha = c["sha"]
        if self._storage.exists(bug_id):
            return "already collected", None
        if sha in run.known_fix_commits:
            return "already collected as the fix of an issue/PR", None
        if not matches_keywords(c["subject"], cfg.title_keywords):
            return "subject has no keyword", None
        if cfg.exclude_bots and is_bot(c["author_name"], email=c["author_email"]):
            return f"bot author ({c['author_name']})", None

        buggy_sha = run.vcs.find_buggy_commit(run.local_repo, sha)
        diff = run.vcs.get_diff(run.local_repo, buggy_sha, sha)
        reason = source_change_rejection(
            diff.unified_diff, run.project.language.test_patterns, cfg.ignore_paths, cfg.max_source_lines,
        )
        if reason:
            return reason, None
        # 事前フィルタを通ったものだけ API を呼ぶ（1コミット1回）
        if cfg.exclude_pr_commits:
            prs = run.tracker.get_commit_pulls(run.slug, sha)
            if prs:
                return f"belongs to PR #{prs[0]}", None

        committed_at = datetime.fromisoformat(c["date"])
        source = IssueInfo(
            tracker_type="github",
            tracker_url=f"https://github.com/{run.slug}",
            issue_id=sha,
            issue_url=f"https://github.com/{run.slug}/commit/{sha}",
            title=c["subject"],
            body=c["body"],
            labels=[],
            reporter=UserInfo(login=c["author_name"]),
            created_at=committed_at,
            closed_at=committed_at,
            kind=KIND_COMMIT,
        )
        if run.dry_run:
            return "", self._dry_record(bug_id, source, run.project, sha, None)
        return "", self._finish_record(
            bug_id=bug_id, source=source, pr_info=None, fix_sha=sha, buggy_sha=buggy_sha,
            diff=diff, project=run.project, local_repo=run.local_repo, vcs=run.vcs, validator=run.validator,
            classifier=run.classifier, checker=run.checker, checks=cfg.fix_checks,
        )

    # ------------------------------------------------------------------
    # 共通の後段処理
    # ------------------------------------------------------------------

    def _finish_record(
        self,
        bug_id: str,
        source: IssueInfo,
        pr_info: PRInfo | None,
        fix_sha: str,
        buggy_sha: str,
        diff: DiffInfo,
        project: ProjectConfig,
        local_repo: Path,
        vcs: GitAdapter,
        validator: Validator,
        classifier: LLMAdapter | None,
        checker: FixChecker | None,
        checks: FixCheckConfig,
    ) -> BugRecord:
        commit = vcs.get_commit_info(local_repo, fix_sha)
        language = vcs.detect_language(local_repo)

        fix = FixInfo(
            commit=fix_sha,
            message=commit.message,
            author_name=commit.author_name,
            committed_at=commit.committed_at,
            diff=diff.unified_diff,
            changed_files=diff.changed_files,
            lines_added=diff.lines_added,
            lines_deleted=diff.lines_deleted,
        )
        repo_info = RepoInfo(
            vcs_type="git",
            url=project.repo_url,
            buggy_commit=buggy_sha,
            fix_commit=fix_sha,
            buggy_files=diff.changed_files,
        )
        environment = EnvironmentInfo(language=language or project.language.type)

        # --- 修正の機械的な確認（fix_checks） ---
        fix_checks = {}
        if checker is not None and checks.any_enabled():
            source_diff, _ = split_diff(diff.unified_diff, project.language.test_patterns)
            # PRはマージ前のCI（headのSHA）で判断する。マージ後のコミットのCIは、後からnightlyの新しい
            # lint等で落ちることがあり修正の良し悪しと関係しない（serde #3034: head=success / merge=failure）
            ci_shas = [pr_info.head_sha] if pr_info and pr_info.head_sha else [fix_sha]
            fix_checks = checker.run(checks, fix_sha, buggy_sha, source_diff, ci_shas=ci_shas)
            for name, result in fix_checks.items():
                logger.debug(f"  [{bug_id}] check {name}: {result.passed} ({result.detail})")
        rejected = rejected_checks(fix_checks, checks)

        classification: BugClassificationInfo | None = None
        if rejected:
            logger.info(f"  [{bug_id}] rejected by fix checks: {', '.join(rejected)}")
            validation = ValidationResult(status=FIX_REJECTED)
        else:
            # --- バグ判定（llm.classify_bugs） ---
            if classifier is not None:
                classification = self._classify(bug_id, source, pr_info, fix, project, classifier)

            # --- テスト検証 ---
            if classification is not None and not classification.is_bug:
                validation = ValidationResult(status=NOT_BUG)
            else:
                validation = validator.validate(
                    repo_path=local_repo,
                    fix_ref=fix_sha,
                    unified_diff=diff.unified_diff,
                    bug_id=bug_id,
                )

        return BugRecord(
            id=bug_id,
            source=source,
            repository=repo_info,
            reproduction=ReproInfo(environment=environment),
            fix=fix,
            metadata=MetaInfo(
                collected_at=datetime.now(timezone.utc),
                collector_version=_VERSION,
            ),
            pr=pr_info,
            validation=validation,
            bug_classification=classification,
            fix_checks=fix_checks,
        )

    def _classify(
        self, bug_id: str, issue: IssueInfo, pr_info: PRInfo | None, fix: FixInfo, project: ProjectConfig,
        classifier: LLMAdapter,
    ) -> BugClassificationInfo:
        """LLMにバグ修正かを判定させる。失敗時は例外（レコードは保存されず次回再判定される）。"""
        source_diff, _ = split_diff(fix.diff, project.language.test_patterns)
        limit = project.llm.max_context_chars
        if pr_info is not None and pr_info.pr_number:
            fix_title = pr_info.title
        else:
            fix_title = (fix.message.splitlines() or [""])[0]
        task = TestGenerationTask(
            issue_title=issue.title,
            issue_body=_truncate(issue.body or "", limit),
            source_diff=_truncate(source_diff, limit),
            test_patterns=project.language.test_patterns,
            language=project.language.type,
            test_command=project.language.test_command,
            bug_id=bug_id,
            fix_title=fix_title,
            source_kind=getattr(issue, "kind", "issue"),
        )
        try:
            verdict = classifier.classify_bug(task)
        except Exception as e:
            raise RuntimeError(f"bug classification failed: {e}") from e
        label = "bug" if verdict.is_bug else f"NOT a bug ({verdict.category})"
        logger.info(f"  [{bug_id}] classification: {label}: {verdict.reason}")
        return BugClassificationInfo(
            is_bug=verdict.is_bug,
            category=verdict.category,
            reason=verdict.reason,
            provider=classifier.provider,
            model=verdict.model,
            assessed_at=datetime.now(timezone.utc),
        )

    def _dry_record(
        self, bug_id: str, source: IssueInfo, project: ProjectConfig, fix_sha: str, pr_info: PRInfo | None,
    ) -> BugRecord:
        """dry-run 用: diff取得・確認・判定・検証を行わない最小限のレコード（保存はされない）。"""
        return BugRecord(
            id=bug_id,
            source=source,
            repository=RepoInfo(vcs_type="git", url=project.repo_url, buggy_commit="", fix_commit=fix_sha),
            reproduction=ReproInfo(environment=EnvironmentInfo(language=project.language.type)),
            fix=FixInfo(
                commit=fix_sha, message="", author_name="",
                committed_at=datetime.now(timezone.utc), diff="",
            ),
            metadata=MetaInfo(collected_at=datetime.now(timezone.utc), collector_version=_VERSION),
            pr=pr_info,
            validation=ValidationResult(status=SKIPPED),
        )

    def _build_record_no_pr(
        self, bug_id: str, issue, project: ProjectConfig, dry_run: bool
    ) -> BugRecord:
        """PR が紐付いていないが収集対象にする場合の最低限レコード。"""
        return BugRecord(
            id=bug_id,
            source=issue,
            repository=RepoInfo(
                vcs_type="git", url=project.repo_url,
                buggy_commit="", fix_commit="",
            ),
            reproduction=ReproInfo(environment=EnvironmentInfo(language=project.language.type)),
            fix=FixInfo(
                commit="", message="", author_name="",
                committed_at=datetime.now(timezone.utc), diff="",
            ),
            metadata=MetaInfo(
                collected_at=datetime.now(timezone.utc),
                collector_version=_VERSION,
            ),
            validation=ValidationResult(status=SKIPPED),
        )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"
