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
    head_sha: str = ""  # PRブランチ先頭のSHA（CI結果の確認に使う。不明なら空）
    # PR起点の収集で判定したマージ方式（merge / squash / rebase）。Issue起点では空
    merge_method: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> PRInfo:
        return cls(
            pr_number=d["pr_number"],
            pr_url=d["pr_url"],
            title=d["title"],
            merge_commit=d["merge_commit"],
            head_sha=d.get("head_sha", ""),
            merge_method=d.get("merge_method", ""),
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
# LLMによる再現テスト生成結果
# ---------------------------------------------------------------------------

@dataclass
class GeneratedTestInfo:
    """LLMが合成した再現テストの情報（doc/test-generation.md 参照）。"""
    file_path: str            # テストファイルパス（リポジトリルートからの相対パス）
    function_name: str | None  # 生成されたテスト関数/メソッド名
    source_code: str          # 生成されたテストファイルの内容
    framework: str            # 言語種別（"rust" / "python" / "java" 等）
    provider: str             # LLMAdapter の実装名（"claude_code" 等）
    model: str                # 使用したモデル名（取得できなければ空文字）
    attempts: int              # confirmed に至るまでに要した試行回数
    confirmed: bool            # buggy:FAIL / fix:PASS を機械的に確認できたか
    generated_at: datetime

    @classmethod
    def from_dict(cls, d: dict) -> GeneratedTestInfo:
        return cls(
            file_path=d["file_path"],
            function_name=d.get("function_name"),
            source_code=d["source_code"],
            framework=d["framework"],
            provider=d["provider"],
            model=d.get("model", ""),
            attempts=d["attempts"],
            confirmed=d["confirmed"],
            generated_at=datetime.fromisoformat(d["generated_at"]),
        )


@dataclass
class TestabilityInfo:
    """
    再現テスト生成前に、LLMに「このバグはテストで再現可能か」を判定させた結果
    （doc/test-generation.md 3.1節）。testable=False のレコードは生成対象から外す。
    """
    testable: bool
    category: str             # TESTABILITY_CATEGORIES のいずれか
    reason: str               # LLMによる判定理由
    provider: str
    model: str
    assessed_at: datetime

    @classmethod
    def from_dict(cls, d: dict) -> TestabilityInfo:
        return cls(
            testable=d["testable"],
            category=d.get("category", ""),
            reason=d.get("reason", ""),
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            assessed_at=datetime.fromisoformat(d["assessed_at"]),
        )


# TestabilityInfo.category の値
TESTABILITY_CATEGORIES = (
    "program_bug",              # プログラムのロジック起因で、テストで再現できる
    "not_program_bug",          # ドキュメント・CI・ビルド設定・依存更新等、プログラム起因でない
    "environment_dependent",    # 特定OS/端末/ネットワーク/タイミング等に依存し、テストで安定して再現できない
    "insufficient_information", # Issue・diffから振る舞いの違いを特定できない
)


@dataclass
class BugClassificationInfo:
    """
    収集時に、LLMに「この Issue はバグ報告で、特定した変更はその修正か」を判定させた結果
    （doc/bug-classification.md）。is_bug=False のレコードはテスト検証せず validation.status=not_bug で保存する。
    """
    is_bug: bool
    category: str             # BUG_CATEGORIES のいずれか
    reason: str
    provider: str
    model: str
    assessed_at: datetime

    @classmethod
    def from_dict(cls, d: dict) -> BugClassificationInfo:
        return cls(
            is_bug=d["is_bug"],
            category=d.get("category", ""),
            reason=d.get("reason", ""),
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            assessed_at=datetime.fromisoformat(d["assessed_at"]),
        )


@dataclass
class FixCheckResult:
    """修正の機械的な確認1件の結果（doc/fix-checks.md）。"""
    passed: bool | None   # None は判定不能（CI結果が無い等）。exclude モードでも除外しない
    detail: str           # 人が読む要約
    data: dict = field(default_factory=dict)  # 確認ごとの詳細（リバートしたコミット、タグ名等）

    @classmethod
    def from_dict(cls, d: dict) -> FixCheckResult:
        return cls(passed=d.get("passed"), detail=d.get("detail", ""), data=d.get("data", {}) or {})


# BugClassificationInfo.category の値
BUG_CATEGORIES = (
    "bug",
    "feature_request",
    "question",
    "documentation",
    "maintenance",
    "unrelated_fix",   # Issueはバグだが、特定した変更がその修正ではない
    "other",
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
LLM_VALID = "llm_valid"   # LLMが合成した再現テストでFAIL→PASSを確認（バグ認定）
NOT_BUG = "not_bug"       # 収集時のLLM判定でバグ修正ではないとされた（テスト検証せず）
FIX_REJECTED = "fix_rejected"  # exclude モードの修正確認（fix_checks）に落ちた（テスト検証せず）


@dataclass
class ValidationResult:
    status: str
    fix_result: TestRunResult | None = None
    buggy_result: TestRunResult | None = None

    @property
    def is_confirmed_bug(self) -> bool:
        return self.status in (VALID, LLM_VALID)

    @classmethod
    def from_dict(cls, d: dict) -> ValidationResult:
        return cls(
            status=d["status"],
            fix_result=TestRunResult.from_dict(d["fix_result"]) if d.get("fix_result") else None,
            # 旧データに残っている "coverage" キー（カバレッジ計測は削除済み）は無視する
            buggy_result=TestRunResult.from_dict(d["buggy_result"]) if d.get("buggy_result") else None,
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
    generated_test: GeneratedTestInfo | None = None
    testability: TestabilityInfo | None = None

    @classmethod
    def from_dict(cls, d: dict) -> ReproInfo:
        return cls(
            environment=EnvironmentInfo.from_dict(d["environment"]),
            steps=d.get("steps", []),
            expected_behavior=d.get("expected_behavior"),
            actual_behavior=d.get("actual_behavior"),
            generated_test=GeneratedTestInfo.from_dict(d["generated_test"]) if d.get("generated_test") else None,
            testability=TestabilityInfo.from_dict(d["testability"]) if d.get("testability") else None,
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
    bug_classification: BugClassificationInfo | None = None
    # 修正の機械的な確認の結果（確認名 → 結果）。実行しなかった確認は含まれない
    fix_checks: dict[str, FixCheckResult] = field(default_factory=dict)

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
            bug_classification=(
                BugClassificationInfo.from_dict(d["bug_classification"]) if d.get("bug_classification") else None
            ),
            fix_checks={k: FixCheckResult.from_dict(v) for k, v in (d.get("fix_checks") or {}).items()},
        )
