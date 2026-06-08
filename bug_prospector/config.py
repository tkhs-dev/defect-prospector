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
        )


def _env(value: str) -> str:
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1], "")
    return value or ""
