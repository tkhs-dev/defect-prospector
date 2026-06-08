from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import git

from ...core.models.commit import CommitInfo, DiffInfo
from .base import VCSAdapter

logger = logging.getLogger(__name__)

_LANG_INDICATORS: dict[str, list[str]] = {
    "python": ["pyproject.toml", "requirements.txt", "setup.py", "setup.cfg"],
    "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
    "javascript": ["package.json"],
    "typescript": ["tsconfig.json"],
    "ruby": ["Gemfile"],
    "go": ["go.mod"],
    "rust": ["Cargo.toml"],
}

_EXT_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".java": "java",
    ".js": "javascript",
    ".ts": "typescript",
    ".rb": "ruby",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".cpp": "c++",
}


class GitAdapter(VCSAdapter):

    @property
    def vcs_type(self) -> str:
        return "git"

    def ensure_repo(self, url: str, dest: Path) -> git.Repo:
        if dest.exists() and (dest / ".git").exists():
            repo = git.Repo(dest)
            try:
                repo.remote("origin").fetch()
                logger.debug(f"Fetched {dest}")
            except git.GitCommandError as e:
                logger.warning(f"Fetch failed (using cached): {e}")
            return repo
        dest.mkdir(parents=True, exist_ok=True)
        logger.info(f"Cloning {url} → {dest}")
        return git.Repo.clone_from(url, dest)

    def get_commit_info(self, repo_path: Path, ref: str) -> CommitInfo:
        repo = git.Repo(repo_path)
        c = repo.commit(ref)
        changed_files: list[str] = []
        if c.parents:
            changed_files = [d.a_path for d in c.diff(c.parents[0])]
        return CommitInfo(
            ref=c.hexsha,
            message=c.message.strip(),
            author_name=c.author.name or "",
            author_email=c.author.email or "",
            committed_at=datetime.fromtimestamp(c.committed_date, tz=timezone.utc),
            parent_refs=[p.hexsha for p in c.parents],
            changed_files=changed_files,
        )

    def get_diff(self, repo_path: Path, from_ref: str, to_ref: str) -> DiffInfo:
        repo = git.Repo(repo_path)
        diff_text: str = repo.git.diff(from_ref, to_ref)
        name_only: str = repo.git.diff("--name-only", from_ref, to_ref)
        changed_files = [f for f in name_only.splitlines() if f]

        lines_added = sum(
            1 for line in diff_text.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        lines_deleted = sum(
            1 for line in diff_text.splitlines()
            if line.startswith("-") and not line.startswith("---")
        )

        return DiffInfo(
            from_ref=from_ref,
            to_ref=to_ref,
            unified_diff=diff_text,
            changed_files=changed_files,
            lines_added=lines_added,
            lines_deleted=lines_deleted,
        )

    def find_buggy_commit(self, repo_path: Path, fix_ref: str) -> str:
        repo = git.Repo(repo_path)
        fix_commit = repo.commit(fix_ref)
        if fix_commit.parents:
            return fix_commit.parents[0].hexsha
        return fix_commit.hexsha

    def detect_language(self, repo_path: Path) -> str | None:
        for lang, indicators in _LANG_INDICATORS.items():
            if any((repo_path / f).exists() for f in indicators):
                return lang
        # Fallback: most frequent source file extension
        try:
            ext_counts: dict[str, int] = {}
            for p in repo_path.rglob("*"):
                if not p.is_file():
                    continue
                if any(part.startswith(".") for part in p.relative_to(repo_path).parts):
                    continue
                ext = p.suffix.lower()
                if ext in _EXT_TO_LANG:
                    ext_counts[ext] = ext_counts.get(ext, 0) + 1
            if ext_counts:
                best_ext = max(ext_counts, key=lambda e: ext_counts[e])
                return _EXT_TO_LANG[best_ext]
        except Exception:
            pass
        return None
