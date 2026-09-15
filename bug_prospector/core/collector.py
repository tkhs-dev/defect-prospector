"""
収集オーケストレーター。

フロー:
  1. GitHub で Issue 検索
  2. 各 Issue に紐付いたマージ済み PR を取得（Timeline API 優先）
  3. PR の merge_commit を fix_commit、その parent を buggy_commit として差分取得
  4. validation.enabled の場合: リバースパッチ戦略でテスト検証
  5. BugRecord を Storage に保存（二重取得はスキップ）
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..adapters.tracker.github import GitHubAdapter
from ..adapters.vcs.git import GitAdapter
from ..config import ProjectConfig
from .models.environment import EnvironmentInfo
from .models.query import IssueQuery
from .models.record import (
    BugRecord,
    FixInfo,
    MetaInfo,
    PRInfo,
    RepoInfo,
    ReproInfo,
    ValidationResult,
    SKIPPED,
)
from .storage import Storage
from .validator import Validator

logger = logging.getLogger(__name__)

_VERSION = "0.1.0"


class CollectionInterrupted(Exception):
    """Ctrl+C による中断時に中途カウントを運ぶ例外。"""
    def __init__(self, new: int, skipped: int, failed: int) -> None:
        self.new = new
        self.skipped = skipped
        self.failed = failed


class Collector:

    def __init__(self, storage: Storage, dataset_dir: Path) -> None:
        self._storage = storage
        self._dataset_dir = dataset_dir

    def run_project(
        self,
        project: ProjectConfig,
        limit_override: int | None = None,
        dry_run: bool = False,
    ) -> tuple[int, int, int]:
        """
        ProjectConfig に従って収集を実行する。
        Returns: (new, skipped, failed)
        """
        tracker = GitHubAdapter(token=project.tracker_auth.token)
        vcs = GitAdapter()

        slug = GitHubAdapter.parse_slug(project.tracker_url)
        repo_slug_flat = slug.replace("/", "-")
        local_repo = self._dataset_dir / "repos" / repo_slug_flat

        val = project.validation
        worktree_path = self._dataset_dir / "worktrees" / repo_slug_flat if val.enabled else None
        patch_dir = self._dataset_dir / "patches" / project.project_id if val.enabled else None
        test_log_dir = self._dataset_dir / "test-logs" / project.project_id if val.enabled and val.save_test_output else None
        validator = Validator(
            project.language, val,
            worktree_path=worktree_path, patch_dir=patch_dir, test_log_dir=test_log_dir,
            coverage_config=project.coverage,
        )

        if not dry_run:
            vcs.ensure_repo(project.repo_url, local_repo)

        search = project.search
        since_dt = _parse_dt(search.since)
        until_dt = _parse_dt(search.until)
        effective_limit = limit_override if limit_override is not None else search.limit

        query = IssueQuery(
            repo_url=project.tracker_url,
            labels=search.labels,
            state=search.state,
            linked_pr=search.require_pr,
            limit=effective_limit,
            since=since_dt,
            until=until_dt,
        )
        issues = tracker.search_bugs(query)
        logger.info(f"Found {len(issues)} issues")

        new_count = skipped_count = failed_count = 0

        try:
            for issue in issues:
                bug_id = f"github-{repo_slug_flat}-{issue.issue_id}"

                if self._storage.exists(bug_id):
                    skipped_count += 1
                    logger.debug(f"  [{bug_id}] already collected, skipping")
                    continue

                try:
                    record = self._build_record(
                        bug_id=bug_id,
                        issue=issue,
                        project=project,
                        slug=slug,
                        local_repo=local_repo,
                        tracker=tracker,
                        vcs=vcs,
                        validator=validator,
                        dry_run=dry_run,
                    )
                except Exception as e:
                    failed_count += 1
                    logger.warning(f"  [{bug_id}] failed: {e}")
                    continue

                if record is None:
                    # require_pr かつ PR なし → スキップ（カウントは failed ではなく skipped）
                    skipped_count += 1
                    continue

                if not dry_run:
                    self._storage.save(record)

                new_count += 1
                val_status = record.validation.status if record.validation else "no-validation"
                logger.info(f"  [{bug_id}] OK  validation={val_status}")
        except KeyboardInterrupt:
            raise CollectionInterrupted(new_count, skipped_count, failed_count)
        finally:
            validator.close()

        return new_count, skipped_count, failed_count

    # ------------------------------------------------------------------
    # private
    # ------------------------------------------------------------------

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
            fix = FixInfo(
                commit=fix_sha, message="", author_name="",
                committed_at=datetime.now(timezone.utc), diff="",
            )
            repo_info = RepoInfo(
                vcs_type="git", url=project.repo_url,
                buggy_commit="", fix_commit=fix_sha,
            )
            environment = EnvironmentInfo(language=project.language.type)
            validation = ValidationResult(status=SKIPPED)
        else:
            buggy_sha = vcs.find_buggy_commit(local_repo, fix_sha)
            diff = vcs.get_diff(local_repo, buggy_sha, fix_sha)
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

            # --- テスト検証 ---
            validation = validator.validate(
                repo_path=local_repo,
                fix_ref=fix_sha,
                unified_diff=diff.unified_diff,
                bug_id=bug_id,
            )

        return BugRecord(
            id=bug_id,
            source=issue,
            repository=repo_info,
            reproduction=ReproInfo(environment=environment),
            fix=fix,
            metadata=MetaInfo(
                collected_at=datetime.now(timezone.utc),
                collector_version=_VERSION,
            ),
            pr=pr_info,
            validation=validation,
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
