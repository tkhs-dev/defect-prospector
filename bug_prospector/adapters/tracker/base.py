from __future__ import annotations

from abc import ABC, abstractmethod

from ...core.models.issue import IssueInfo
from ...core.models.query import IssueQuery


class IssueTrackerAdapter(ABC):

    @property
    @abstractmethod
    def tracker_type(self) -> str: ...

    @abstractmethod
    def search_bugs(self, query: IssueQuery) -> list[IssueInfo]: ...

    @abstractmethod
    def get_fix_commit(self, issue_number: int, repo_slug: str) -> str | None:
        """Issueを閉じたコミット/マージコミットのSHAを返す。見つからない場合はNone。"""
