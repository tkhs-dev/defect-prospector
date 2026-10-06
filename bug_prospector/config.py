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
class CoverageConfig:
    """カバレッジ計測設定。validation.enabled=true の場合のみ有効。"""
    enabled: bool = False
    # 参考情報。空なら言語アダプタのデフォルトツールを使う（rust: cargo-llvm-cov, java: jacoco）
    tool: str = ""
    # 空なら言語アダプタのデフォルトコマンドを使う
    command: str = ""
    # レポートファイルの相対パス。空なら言語アダプタのデフォルトパスを使う
    # （マルチモジュールJavaプロジェクト等、デフォルトが合わない場合に上書きする）
    report_path: str = ""
    timeout: int = 600


@dataclass
class ClaudeCodeConfig:
    """provider: claude_code のときのみ参照する設定。"""
    command: str = "claude"  # PATH上のclaudeコマンド名、またはフルパス
    # claude -p に渡す --allowedTools のうち、Bash(test_command) を除く固定部分
    allowed_tools: str = "Read,Grep,Glob,Write,Edit"
    timeout: int = 600


@dataclass
class LLMConfig:
    """LLMによる再現テスト生成設定（doc/test-generation.md 参照）。"""
    enabled: bool = False
    provider: str = "claude_code"
    max_attempts: int = 3
    # プロンプトに埋め込むコンテキスト（Issue本文・diff等）1ブロックあたりの文字数上限
    max_context_chars: int = 20000
    claude_code: ClaudeCodeConfig = field(default_factory=ClaudeCodeConfig)


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
    coverage: CoverageConfig = field(default_factory=CoverageConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

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
        cov_d = d.get("coverage", {})
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
        coverage = CoverageConfig(
            enabled=cov_d.get("enabled", False),
            tool=cov_d.get("tool", ""),
            command=cov_d.get("command", ""),
            report_path=cov_d.get("report_path", ""),
            timeout=cov_d.get("timeout", 600),
        )
        claude_code_d = llm_d.get("claude_code", {})
        llm = LLMConfig(
            enabled=llm_d.get("enabled", False),
            provider=llm_d.get("provider", "claude_code"),
            max_attempts=llm_d.get("max_attempts", 3),
            max_context_chars=llm_d.get("max_context_chars", 20000),
            claude_code=ClaudeCodeConfig(
                command=claude_code_d.get("command", "claude"),
                allowed_tools=claude_code_d.get("allowed_tools", "Read,Grep,Glob,Write,Edit"),
                timeout=claude_code_d.get("timeout", 600),
            ),
        )

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
            coverage=coverage,
            llm=llm,
        )


def _env(value: str) -> str:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value or ""
