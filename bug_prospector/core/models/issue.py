from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class UserInfo:
    login: str
    display_name: str | None = None
    url: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> UserInfo:
        return cls(
            login=d["login"],
            display_name=d.get("display_name"),
            url=d.get("url"),
        )


# IssueInfo.kind の値: レコードの出自（doc/pr-collection.md）
KIND_ISSUE = "issue"                # Issue起点（独立したバグ報告がある）
KIND_PULL_REQUEST = "pull_request"  # PR起点（Issueは無く、PRのタイトル・本文が報告の代わり）
KIND_COMMIT = "commit"              # 直接コミット（PRも無く、コミットメッセージのみ）
SOURCE_KINDS = (KIND_ISSUE, KIND_PULL_REQUEST, KIND_COMMIT)


@dataclass
class IssueInfo:
    """
    バグの出自情報。kind=issue なら Issue、pull_request なら PR、commit なら修正コミットの情報で埋める
    （issue_id は Issue/PR 番号またはコミットSHA、issue_url はそれぞれのURL）。
    """
    tracker_type: str
    tracker_url: str
    issue_id: str
    issue_url: str
    title: str
    body: str
    labels: list[str]
    reporter: UserInfo
    created_at: datetime
    severity: str | None = None
    assignees: list[UserInfo] = field(default_factory=list)
    closed_at: datetime | None = None
    linked_prs: list[str] = field(default_factory=list)
    kind: str = KIND_ISSUE

    @classmethod
    def from_dict(cls, d: dict) -> IssueInfo:
        return cls(
            tracker_type=d["tracker_type"],
            tracker_url=d["tracker_url"],
            issue_id=d["issue_id"],
            issue_url=d["issue_url"],
            title=d["title"],
            body=d["body"],
            labels=d["labels"],
            reporter=UserInfo.from_dict(d["reporter"]),
            created_at=_dt(d["created_at"]),
            severity=d.get("severity"),
            assignees=[UserInfo.from_dict(a) for a in d.get("assignees", [])],
            closed_at=_dt(d.get("closed_at")),
            linked_prs=d.get("linked_prs", []),
            kind=d.get("kind", KIND_ISSUE),
        )


def _dt(v: str | None) -> datetime | None:
    if v is None:
        return None
    return datetime.fromisoformat(v)
