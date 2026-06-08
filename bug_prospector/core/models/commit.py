from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CommitInfo:
    ref: str
    message: str
    author_name: str
    author_email: str
    committed_at: datetime
    parent_refs: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> CommitInfo:
        return cls(
            ref=d["ref"],
            message=d["message"],
            author_name=d["author_name"],
            author_email=d["author_email"],
            committed_at=datetime.fromisoformat(d["committed_at"]),
            parent_refs=d.get("parent_refs", []),
            changed_files=d.get("changed_files", []),
        )


@dataclass
class DiffInfo:
    from_ref: str
    to_ref: str
    unified_diff: str
    changed_files: list[str] = field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0
