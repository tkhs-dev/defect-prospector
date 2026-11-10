from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# ---------------------------------------------------------------------------
# Sub-configs
# ---------------------------------------------------------------------------

@dataclass
class TrackerAuth:
    token: str = ""


@dataclass
class SearchConfig:
    """課題管理システムでの検索条件。"""
    labels: list[str] = field(default_factory=lambda: ["bug"])
    state: str = "closed"
    limit: int | None = None
    since: str | None = None
    until: str | None = None
    # True の場合、マージ済みPRが紐付いていないIssueはスキップする
    require_pr: bool = True


@dataclass
class LanguageConfig:
    """言語・テスト実行設定。"""
    type: str = "python"
    # テストファイルと判定するパターン（split_diff で使用）
    test_patterns: list[str] = field(default_factory=lambda: [
        "tests/**", "test/**", "test_*.py", "*_test.py",
    ])
    # テスト実行前に一度だけ走らせる環境セットアップコマンド
    setup_commands: list[str] = field(default_factory=list)
    # テスト実行コマンド
    test_command: str = "pytest --tb=short -q"
    # テスト実行タイムアウト（秒）
    test_timeout: int = 120


@dataclass
class ValidationConfig:
    """テストによるバグ検証設定。"""
    enabled: bool = False
    # "reverse_patch_source" : ソースファイルのみ逆パッチ（テストは保持）
    # "reverse_patch_all"    : 全ファイルを逆パッチ
    strategy: str = "reverse_patch_source"
    # True にするとテスト実行の全出力を dataset/test-logs/ に保存する
    save_test_output: bool = False
    # True にすると失敗したテストケース名をログに出力する
    show_failed_tests: bool = False


@dataclass
class ClaudeCodeConfig:
    """provider: claude_code のときのみ参照する設定。"""
    command: str = "claude"  # PATH上のclaudeコマンド名、またはフルパス
    # claude -p に渡す --allowedTools のうち、Bash(test_command) を除く固定部分
    allowed_tools: str = "Read,Grep,Glob,Write,Edit"
    timeout: int = 600
    # Rust向けのボトムアップ構築ガイダンス（RUG由来、doc/rug-integration-note.md）をプロンプトに付けるか。
    # false にすると「全コンテキストを渡して一発で書かせる」ワンショット形式になる（比較実験用）
    rust_bottom_up: bool = True


@dataclass
class LLMConfig:
    """LLMによる再現テスト生成設定（doc/test-generation.md 参照）。"""
    enabled: bool = False
    provider: str = "claude_code"
    max_attempts: int = 3
    # プロンプトに埋め込むコンテキスト（Issue本文・diff等）1ブロックあたりの文字数上限
    max_context_chars: int = 20000
    # true: fixにテストが同梱されていても、それがFAIL→PASSを確認できていなければ
    # （＝有効なテストが付属していなければ）生成対象にする。false: テスト非同梱のfixのみ対象
    include_fixes_with_tests: bool = False
    # true: 生成前にLLMへ「テストで再現可能か」を尋ね、不可能と答えたレコードを生成対象から外す
    screening: bool = False
    # true: bp collect 時にLLMへ「バグ報告とその修正か」を尋ね、そうでないものは not_bug として
    # テスト検証せずに記録する（ラベルでバグを管理していないプロジェクト向け。llm.enabled とは独立）
    classify_bugs: bool = False
    claude_code: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)


# ---------------------------------------------------------------------------
# 修正の機械的な確認（doc/fix-checks.md）
# ---------------------------------------------------------------------------

# 各確認のモード
CHECK_OFF = "off"          # 実行しない
CHECK_RECORD = "record"    # 実行して結果を BugRecord.fix_checks に記録するだけ
CHECK_EXCLUDE = "exclude"  # 記録したうえで、確認に落ちたレコードを fix_rejected として検証対象から外す
CHECK_MODES = (CHECK_OFF, CHECK_RECORD, CHECK_EXCLUDE)

FIX_CHECK_NAMES = ("revert", "follow_up_fix", "release", "ci")


@dataclass
class FixCheckConfig:
    """修正の機械的な確認の設定。各値は off / record / exclude。"""
    revert: str = CHECK_OFF          # 後続のコミットでリバートされていないか
    follow_up_fix: str = CHECK_OFF   # 修正で追加した行が短期間のうちに書き換えられていないか
    follow_up_window_days: int = 30
    release: str = CHECK_OFF         # 修正コミットを含むタグ（リリース）があるか
    ci: str = CHECK_OFF              # 修正コミット（PRならPRのhead）のCIが失敗していないか

    def mode(self, name: str) -> str:
        return getattr(self, name)

    def any_enabled(self) -> bool:
        return any(self.mode(n) != CHECK_OFF for n in FIX_CHECK_NAMES)

    @classmethod
    def from_dict(cls, d: dict | None, base: FixCheckConfig | None = None) -> FixCheckConfig:
        """d のキーを base（省略時は既定値）に上書きして作る。収集元ごとの上書きに使う。"""
        cfg = FixCheckConfig(**vars(base)) if base else FixCheckConfig()
        for key, value in (d or {}).items():
            if key not in cls.__dataclass_fields__:
                raise ValueError(f"Unknown fix_checks key: {key!r}")
            if key in FIX_CHECK_NAMES:
                if value is True:
                    value = CHECK_RECORD
                elif value is False:
                    value = CHECK_OFF
                if value not in CHECK_MODES:
                    raise ValueError(f"fix_checks.{key} must be one of {CHECK_MODES}, got {value!r}")
            setattr(cfg, key, value)
        return cfg


# ---------------------------------------------------------------------------
# 収集元（doc/pr-collection.md）
# ---------------------------------------------------------------------------

# PR・直接コミット収集で「ソースの変更ではない」とみなすパス（これだけを変更する修正は除外）
DEFAULT_IGNORE_PATHS = [
    "*.md", "*.txt", "*.rst", "LICENSE*", "docs/**", "doc/**",
    ".github/**", ".gitignore", "*.yml", "*.yaml", "*.toml", "*.lock",
]


@dataclass
class IssueSourceConfig:
    """Issue起点の収集（検索条件は search: を使う）。"""
    enabled: bool = True
    fix_checks: FixCheckConfig = field(default_factory=FixCheckConfig)


@dataclass
class PullRequestSourceConfig:
    """PR起点の収集: Issueに紐付かないマージ済みPRを修正として扱う。"""
    enabled: bool = False
    labels: list[str] = field(default_factory=list)      # 空ならラベルで絞らない
    # タイトルがこれらの語（大文字小文字無視、単語単位）を含むものだけ候補にする。None なら絞らない
    title_keywords: list[str] | None = None
    exclude_bots: bool = True
    # ソース差分（テスト以外）の行数（追加+削除）がこれを超えるものは除外。None なら上限なし
    max_source_lines: int | None = 2000
    ignore_paths: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_PATHS))
    limit: int | None = None
    fix_checks: FixCheckConfig = field(default_factory=FixCheckConfig)


@dataclass
class CommitSourceConfig:
    """直接コミットの収集: デフォルトブランチの first-parent 上の非マージコミットのうち、PRに属さないもの。"""
    enabled: bool = False
    title_keywords: list[str] | None = None
    exclude_bots: bool = True
    max_source_lines: int | None = 2000
    ignore_paths: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE_PATHS))
    # true: GitHub API でPRに属すると分かったコミットを除外する（squash/rebase で取り込まれたPR）
    exclude_pr_commits: bool = True
    limit: int | None = None
    fix_checks: FixCheckConfig = field(default_factory=FixCheckConfig)


@dataclass
class SourcesConfig:
    issues: IssueSourceConfig = field(default_factory=IssueSourceConfig)
    pull_requests: PullRequestSourceConfig = field(default_factory=PullRequestSourceConfig)
    commits: CommitSourceConfig = field(default_factory=CommitSourceConfig)


SOURCE_NAMES = ("issues", "pull_requests", "commits")


def _parse_sources(d: dict, default_checks: FixCheckConfig) -> SourcesConfig:
    def checks(sd: dict) -> FixCheckConfig:
        return FixCheckConfig.from_dict(sd.get("fix_checks"), base=default_checks)

    issues_d = d.get("issues", {}) or {}
    prs_d = d.get("pull_requests", {}) or {}
    commits_d = d.get("commits", {}) or {}
    prs_default = PullRequestSourceConfig()
    commits_default = CommitSourceConfig()
    return SourcesConfig(
        issues=IssueSourceConfig(
            enabled=issues_d.get("enabled", True),
            fix_checks=checks(issues_d),
        ),
        pull_requests=PullRequestSourceConfig(
            enabled=prs_d.get("enabled", False),
            labels=prs_d.get("labels", []) or [],
            title_keywords=prs_d.get("title_keywords"),
            exclude_bots=prs_d.get("exclude_bots", True),
            max_source_lines=prs_d.get("max_source_lines", prs_default.max_source_lines),
            ignore_paths=prs_d.get("ignore_paths", prs_default.ignore_paths),
            limit=prs_d.get("limit"),
            fix_checks=checks(prs_d),
        ),
        commits=CommitSourceConfig(
            enabled=commits_d.get("enabled", False),
            title_keywords=commits_d.get("title_keywords"),
            exclude_bots=commits_d.get("exclude_bots", True),
            max_source_lines=commits_d.get("max_source_lines", commits_default.max_source_lines),
            ignore_paths=commits_d.get("ignore_paths", commits_default.ignore_paths),
            exclude_pr_commits=commits_d.get("exclude_pr_commits", True),
            limit=commits_d.get("limit"),
            fix_checks=checks(commits_d),
        ),
    )


# ---------------------------------------------------------------------------
# Top-level project config
# ---------------------------------------------------------------------------

@dataclass
class ProjectConfig:
    """1プロジェクト分の収集設定。"""
    project_id: str
    repo_url: str
    tracker_url: str
    tracker_type: str = "github"
    vcs_type: str = "git"
    tracker_auth: TrackerAuth = field(default_factory=TrackerAuth)
    search: SearchConfig = field(default_factory=SearchConfig)
    language: LanguageConfig = field(default_factory=LanguageConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    # 修正の機械的な確認の既定値（sources.<source>.fix_checks で収集元ごとに上書き済みの値が sources に入る）
    fix_checks: FixCheckConfig = field(default_factory=FixCheckConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)

    @classmethod
    def from_file(cls, path: Path) -> ProjectConfig:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, d: dict) -> ProjectConfig:
        tracker_auth = TrackerAuth(
            token=_env(d.get("tracker", {}).get("auth", {}).get("token", ""))
        )
        search_d = d.get("search", {})
        lang_d = d.get("language", {})
        val_d = d.get("validation", {})
        llm_d = d.get("llm", {})

        search = SearchConfig(
            labels=search_d.get("labels", ["bug"]),
            state=search_d.get("state", "closed"),
            limit=search_d.get("limit"),
            since=search_d.get("since"),
            until=search_d.get("until"),
            require_pr=search_d.get("require_pr", True),
        )
        language = LanguageConfig(
            type=lang_d.get("type", "python"),
            test_patterns=lang_d.get("test_patterns", LanguageConfig.__dataclass_fields__["test_patterns"].default_factory()),  # type: ignore[misc]
            setup_commands=lang_d.get("setup_commands", []),
            test_command=lang_d.get("test_command", "pytest --tb=short -q"),
            test_timeout=lang_d.get("test_timeout", 120),
        )
        validation = ValidationConfig(
            enabled=val_d.get("enabled", False),
            strategy=val_d.get("strategy", "reverse_patch_source"),
            save_test_output=val_d.get("save_test_output", False),
            show_failed_tests=val_d.get("show_failed_tests", False),
        )
        claude_code_d = llm_d.get("claude_code", {})
        llm = LLMConfig(
            enabled=llm_d.get("enabled", False),
            provider=llm_d.get("provider", "claude_code"),
            max_attempts=llm_d.get("max_attempts", 3),
            max_context_chars=llm_d.get("max_context_chars", 20000),
            include_fixes_with_tests=llm_d.get("include_fixes_with_tests", False),
            screening=llm_d.get("screening", False),
            classify_bugs=llm_d.get("classify_bugs", False),
            claude_code=ClaudeCodeConfig(
                command=claude_code_d.get("command", "claude"),
                allowed_tools=claude_code_d.get("allowed_tools", "Read,Grep,Glob,Write,Edit"),
                timeout=claude_code_d.get("timeout", 600),
                rust_bottom_up=claude_code_d.get("rust_bottom_up", True),
            ),
        )

        fix_checks = FixCheckConfig.from_dict(d.get("fix_checks"))
        sources = _parse_sources(d.get("sources", {}) or {}, fix_checks)

        tracker_d = d.get("tracker", {})
        return cls(
            project_id=d.get("project_id", ""),
            repo_url=d.get("repo", {}).get("url", "") or d.get("repo_url", ""),
            tracker_url=tracker_d.get("url", "") or d.get("tracker_url", ""),
            tracker_type=tracker_d.get("type", "github"),
            vcs_type=d.get("repo", {}).get("vcs_type", "git"),
            tracker_auth=tracker_auth,
            search=search,
            language=language,
            validation=validation,
            llm=llm,
            fix_checks=fix_checks,
            sources=sources,
        )


def _env(value: str) -> str:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value or ""
