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
