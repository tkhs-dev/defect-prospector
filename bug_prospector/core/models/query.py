from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class IssueQuery:
    repo_url: str
    labels: list[str] = field(default_factory=lambda: ["bug"])
    state: str = "closed"
    limit: int | None = None
    since: datetime | None = None
    until: datetime | None = None
