from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import git

from ...core.models.commit import CommitInfo, DiffInfo


class VCSAdapter(ABC):

    @property
    @abstractmethod
    def vcs_type(self) -> str: ...

    @abstractmethod
    def ensure_repo(self, url: str, dest: Path) -> git.Repo: ...

    @abstractmethod
    def get_commit_info(self, repo_path: Path, ref: str) -> CommitInfo: ...

    @abstractmethod
    def get_diff(self, repo_path: Path, from_ref: str, to_ref: str) -> DiffInfo: ...

    @abstractmethod
    def find_buggy_commit(self, repo_path: Path, fix_ref: str) -> str: ...

    @abstractmethod
    def detect_language(self, repo_path: Path) -> str | None: ...
