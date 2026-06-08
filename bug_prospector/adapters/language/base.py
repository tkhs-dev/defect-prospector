from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class LanguageAdapter(ABC):

    @property
    @abstractmethod
    def language_name(self) -> str: ...

    @abstractmethod
    def detect(self, repo_path: Path) -> bool:
        """このリポジトリが対象言語で書かれているか判定する。"""

    @abstractmethod
    def default_test_patterns(self) -> list[str]:
        """テストファイルと判定するデフォルトパターン一覧を返す。"""

    @abstractmethod
    def default_test_command(self, repo_path: Path) -> str:
        """デフォルトのテスト実行コマンドを返す。"""

    @abstractmethod
    def default_setup_commands(self, repo_path: Path) -> list[str]:
        """デフォルトの環境セットアップコマンドを返す。"""
