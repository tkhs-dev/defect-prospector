from __future__ import annotations

from datetime import datetime, timezone

import pytest

from bug_prospector.core.models.environment import EnvironmentInfo
from bug_prospector.core.models.issue import IssueInfo, UserInfo
from bug_prospector.core.models.record import (
    BugRecord,
    FixInfo,
    MetaInfo,
    RepoInfo,
    ReproInfo,
)
from bug_prospector.core.storage import Storage


def _record(bug_id: str, tracker_url: str = "https://github.com/example/repo") -> BugRecord:
    now = datetime.now(timezone.utc)
    return BugRecord(
        id=bug_id,
        source=IssueInfo(
            tracker_type="github",
            tracker_url=tracker_url,
            issue_id="1",
            issue_url=f"{tracker_url}/issues/1",
            title="title",
            body="body",
            labels=["bug"],
            reporter=UserInfo(login="someone"),
            created_at=now,
        ),
        repository=RepoInfo(vcs_type="git", url=tracker_url, buggy_commit="a", fix_commit="b"),
        reproduction=ReproInfo(environment=EnvironmentInfo(language="rust")),
        fix=FixInfo(commit="b", message="fix", author_name="someone", committed_at=now, diff=""),
        metadata=MetaInfo(collected_at=now),
    )


@pytest.fixture
def storage(tmp_path) -> Storage:
    return Storage(tmp_path / "dataset")


class TestStorage:
    def test_save_and_exists(self, storage):
        storage.save(_record("bug-1"))
        assert storage.exists("bug-1") is True
        assert storage.exists("bug-2") is False

    def test_delete_removes_record(self, storage):
        storage.save(_record("bug-1"))
        storage.save(_record("bug-2"))

        storage.delete("bug-1")

        assert storage.exists("bug-1") is False
        assert storage.exists("bug-2") is True
        assert storage.count() == 1

    def test_delete_nonexistent_is_noop(self, storage):
        storage.save(_record("bug-1"))
        storage.delete("does-not-exist")
        assert storage.count() == 1

    def test_list_all_after_delete(self, storage):
        storage.save(_record("bug-1"))
        storage.save(_record("bug-2"))
        storage.delete("bug-1")

        remaining = {r.id for r in storage.list_all()}
        assert remaining == {"bug-2"}
