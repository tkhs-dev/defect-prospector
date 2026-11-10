from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TestGenerationTask:
    """LLMAdapter に渡すコンテキスト。

    全フィールドをプロンプトに使うかはAdapter実装次第（Claude Codeのようなエージェント型は
    worktreeを自分でRead/Grep/Globして探索できるため、一部は使わなくてもよい）。
    """
    issue_title: str
    issue_body: str
    source_diff: str              # fix.diff のうちソース差分部分（split_diff の source_diff）
    test_patterns: list[str]      # 生成するテストファイルが満たすべきパスパターン
    language: str
    test_command: str
    sample_test_files: dict[str, str] = field(default_factory=dict)  # 既存テストのサンプル(path -> 内容)
    bug_id: str = ""              # 使用量ログ等でレコードを識別するためのID
    fix_title: str = ""           # 修正PRのタイトル（コミット直接の修正ならコミットメッセージ1行目）
    # レコードの出自（IssueInfo.kind）。issue 以外では issue_title/issue_body に PR本文 or コミットメッセージが入る
    source_kind: str = "issue"


@dataclass
class TestabilityVerdict:
    """LLMAdapter.assess_testability の戻り値。"""
    testable: bool
    category: str
    reason: str
    model: str = ""


@dataclass
class BugVerdict:
    """LLMAdapter.classify_bug の戻り値。"""
    is_bug: bool
    category: str
    reason: str
    model: str = ""


class LLMAdapter(ABC):
    """LLMを使って再現テストを生成するアダプタの抽象基底クラス（doc/test-generation.md 6.1節）。"""

    provider: str = ""

    @abstractmethod
    def generate_test(
        self,
        worktree: Path,
        task: TestGenerationTask,
        feedback: str | None = None,
    ) -> str:
        """
        worktree 内に再現テストを書き込む（新規ファイル作成、または既存テストファイルへの追記）。
        書き込み先は task.test_patterns にマッチするパスにすること
        （split_diff による逆パッチ適用時に巻き込まれないようにするため）。

        実装方式は問わない:
          - エージェント型（Claude Code 等）: worktree をカレントディレクトリとして起動し、
            自身の Read/Write/Edit ツールで直接ファイルを書き換える。
          - チャット補完API型（素の Anthropic/OpenAI API 等）: プロンプトから diff テキストを
            生成し、このメソッド内で git apply して worktree に適用する。

        呼び出し後、呼び出し元は worktree の `git status --porcelain` を見て
        「何が追加・変更されたか」を判定する。

        Returns:
            使用したモデル名（取得できなければ空文字）。
        Raises:
            生成に失敗した場合（プロセスエラー・タイムアウト・空応答等）は例外を送出する。
        """

    def assess_testability(self, task: TestGenerationTask) -> TestabilityVerdict:
        """
        再現テスト生成の前段で、Issue・ソース差分から「このバグはテストで再現可能か」を判定する
        （doc/test-generation.md 3.1節）。プログラム起因でない（ドキュメント・CI・依存更新等）、
        環境依存でテストでは安定して再現できない、といったレコードを生成対象から外すために使う。

        Raises:
            判定に失敗した場合（プロセスエラー・タイムアウト・応答の解釈不能等）は例外を送出する。
        """
        raise NotImplementedError(f"{type(self).__name__} does not support testability screening")

    def classify_bug(self, task: TestGenerationTask) -> BugVerdict:
        """
        収集時に、Issue と修正差分から「バグ報告とその修正か」を判定する（doc/bug-classification.md）。
        ラベルでバグを管理していないプロジェクトから、機能要望・質問・ドキュメント修正等を除くために使う。

        Raises:
            判定に失敗した場合（プロセスエラー・タイムアウト・応答の解釈不能等）は例外を送出する。
        """
        raise NotImplementedError(f"{type(self).__name__} does not support bug classification")
