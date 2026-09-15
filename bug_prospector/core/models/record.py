from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .environment import EnvironmentInfo
from .issue import IssueInfo


# ---------------------------------------------------------------------------
# テストケース単体の結果
# ---------------------------------------------------------------------------

@dataclass
class TestCaseResult:
    name: str
    passed: bool
    duration_seconds: float | None = None
    message: str | None = None  # 失敗メッセージ

    @classmethod
    def from_dict(cls, d: dict) -> TestCaseResult:
        return cls(
            name=d["name"],
            passed=d["passed"],
            duration_seconds=d.get("duration_seconds"),
            message=d.get("message"),
        )


# ---------------------------------------------------------------------------
# PR情報
# ---------------------------------------------------------------------------

@dataclass
class PRInfo:
    pr_number: int
    pr_url: str
    title: str
    merge_commit: str   # merge commit SHA (= fix_commit)

    @classmethod
    def from_dict(cls, d: dict) -> PRInfo:
        return cls(
            pr_number=d["pr_number"],
            pr_url=d["pr_url"],
            title=d["title"],
            merge_commit=d["merge_commit"],
        )


# ---------------------------------------------------------------------------
# テスト実行結果
# ---------------------------------------------------------------------------

@dataclass
class TestRunResult:
    passed: bool
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_seconds: float = 0.0
    test_cases: list[TestCaseResult] = field(default_factory=list)

    @property
    def failed_tests(self) -> list[str]:
        return [tc.name for tc in self.test_cases if not tc.passed]

    @classmethod
    def from_dict(cls, d: dict) -> TestRunResult:
        return cls(
            passed=d["passed"],
            exit_code=d["exit_code"],
            stdout=d.get("stdout", ""),
            stderr=d.get("stderr", ""),
            duration_seconds=d.get("duration_seconds", 0.0),
            test_cases=[TestCaseResult.from_dict(tc) for tc in d.get("test_cases", [])],
        )


# ---------------------------------------------------------------------------
# カバレッジ計測結果
# ---------------------------------------------------------------------------

# status 定数
MEASURED = "measured"
COVERAGE_ERROR = "error"
UNSUPPORTED = "unsupported"


@dataclass
class CoverageResult:
    tool: str            # "cargo-llvm-cov" / "jacoco" 等
    status: str
    overall_line_rate: float | None = None     # プロジェクト全体の行カバレッジ率 (0.0-1.0)
    overall_branch_rate: float | None = None   # プロジェクト全体の分岐カバレッジ率 (0.0-1.0)
    # diff coverage: このバグの修正差分が触れた行のうち、
    # buggyコミット時点の既存テストで計装・カバーされていた行数
    diff_lines_total: int = 0
    diff_lines_covered: int = 0
    diff_coverage_rate: float | None = None    # diff_lines_covered / diff_lines_total
    error: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> CoverageResult:
        return cls(
            tool=d["tool"],
            status=d["status"],
            overall_line_rate=d.get("overall_line_rate"),
            overall_branch_rate=d.get("overall_branch_rate"),
            diff_lines_total=d.get("diff_lines_total", 0),
            diff_lines_covered=d.get("diff_lines_covered", 0),
            diff_coverage_rate=d.get("diff_coverage_rate"),
            error=d.get("error"),
        )


# ---------------------------------------------------------------------------
# 検証結果
# ---------------------------------------------------------------------------

# status 定数
VALID = "valid"           # FAIL→PASS を確認（バグ認定）
INVALID = "invalid"       # 両方PASSまたは両方FAIL（テストが差分に反応しない）
PATCH_FAILED = "patch_failed"
SETUP_FAILED = "setup_failed"
TIMEOUT = "timeout"
ERROR = "error"
SKIPPED = "skipped"


@dataclass
class ValidationResult:
    status: str
    fix_result: TestRunResult | None = None
    buggy_result: TestRunResult | None = None
    coverage: CoverageResult | None = None

    @property
    def is_confirmed_bug(self) -> bool:
        return self.status == VALID

    @classmethod
    def from_dict(cls, d: dict) -> ValidationResult:
        return cls(
            status=d["status"],
            fix_result=TestRunResult.from_dict(d["fix_result"]) if d.get("fix_result") else None,
            buggy_result=TestRunResult.from_dict(d["buggy_result"]) if d.get("buggy_result") else None,
            coverage=CoverageResult.from_dict(d["coverage"]) if d.get("coverage") else None,
        )


# ---------------------------------------------------------------------------
# BugRecord を構成するサブモデル
# ---------------------------------------------------------------------------

@dataclass
class RepoInfo:
    vcs_type: str
    url: str
    buggy_commit: str
    fix_commit: str
    buggy_files: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict) -> RepoInfo:
        return cls(
            vcs_type=d["vcs_type"],
            url=d["url"],
            buggy_commit=d["buggy_commit"],
            fix_commit=d["fix_commit"],
            buggy_files=d.get("buggy_files", []),
        )


@dataclass
class ReproInfo:
    environment: EnvironmentInfo
    steps: list[str] = field(default_factory=list)
    expected_behavior: str | None = None
    actual_behavior: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> ReproInfo:
        return cls(
            environment=EnvironmentInfo.from_dict(d["environment"]),
            steps=d.get("steps", []),
            expected_behavior=d.get("expected_behavior"),
            actual_behavior=d.get("actual_behavior"),
        )


@dataclass
class FixInfo:
    commit: str
    message: str
    author_name: str
    committed_at: datetime
    diff: str
    changed_files: list[str] = field(default_factory=list)
    lines_added: int = 0
    lines_deleted: int = 0

    @classmethod
    def from_dict(cls, d: dict) -> FixInfo:
        return cls(
            commit=d["commit"],
            message=d["message"],
            author_name=d["author_name"],
            committed_at=datetime.fromisoformat(d["committed_at"]),
            diff=d["diff"],
            changed_files=d.get("changed_files", []),
            lines_added=d.get("lines_added", 0),
            lines_deleted=d.get("lines_deleted", 0),
        )


@dataclass
class MetaInfo:
    collected_at: datetime
    collector_version: str = "0.1.0"
    schema_version: str = "1.0"

    @classmethod
    def from_dict(cls, d: dict) -> MetaInfo:
        return cls(
            collected_at=datetime.fromisoformat(d["collected_at"]),
            collector_version=d.get("collector_version", "0.1.0"),
            schema_version=d.get("schema_version", "1.0"),
        )


# ---------------------------------------------------------------------------
# 中心モデル
# ---------------------------------------------------------------------------

@dataclass
class BugRecord:
    id: str
    source: IssueInfo
    repository: RepoInfo
    reproduction: ReproInfo
    fix: FixInfo
    metadata: MetaInfo
    pr: PRInfo | None = None
    validation: ValidationResult | None = None

    @classmethod
    def from_dict(cls, d: dict) -> BugRecord:
        return cls(
            id=d["id"],
            source=IssueInfo.from_dict(d["source"]),
            repository=RepoInfo.from_dict(d["repository"]),
            reproduction=ReproInfo.from_dict(d["reproduction"]),
            fix=FixInfo.from_dict(d["fix"]),
            metadata=MetaInfo.from_dict(d["metadata"]),
            pr=PRInfo.from_dict(d["pr"]) if d.get("pr") else None,
            validation=ValidationResult.from_dict(d["validation"]) if d.get("validation") else None,
        )
