# アダプタインターフェース仕様書 — defect-prospector

## 1. 概要

アダプタは3種類定義する。すべて `abc.ABC` を継承した抽象基底クラスであり、  
コアは常にこのインターフェース経由でアクセスする。

| アダプタ種別 | 抽象クラス | 役割 |
|---|---|---|
| VCS | `VCSAdapter` | リポジトリ操作・コミット取得 |
| 課題管理 | `IssueTrackerAdapter` | Issue検索・詳細取得 |
| 言語 | `LanguageAdapter` | 言語検出・環境情報取得 |

## 2. VCSAdapter

```python
# bug-prospector/adapters/vcs/base.py
from abc import ABC, abstractmethod
from pathlib import Path
from ..models.commit import CommitInfo
from ..models.diff import DiffInfo

class VCSAdapter(ABC):

    @property
    @abstractmethod
    def vcs_type(self) -> str:
        """アダプタが対応するVCS種別 (例: "git", "svn", "hg")"""

    @abstractmethod
    def clone(self, url: str, dest: Path) -> None:
        """リポジトリをdestにクローン/チェックアウトする"""

    @abstractmethod
    def fetch(self, repo_path: Path) -> None:
        """ローカルクローンを最新化する"""

    @abstractmethod
    def checkout(self, repo_path: Path, ref: str) -> None:
        """指定refにワーキングツリーを切り替える"""

    @abstractmethod
    def resolve_ref(self, repo_path: Path, ref: str) -> str:
        """ブランチ名・タグ名をコミットhash/revisionに解決する"""

    @abstractmethod
    def get_commit_info(self, repo_path: Path, ref: str) -> CommitInfo:
        """指定コミットのメタデータを返す"""

    @abstractmethod
    def get_diff(self, repo_path: Path, from_ref: str, to_ref: str) -> DiffInfo:
        """2つのコミット間の差分を返す (unified diff)"""

    @abstractmethod
    def get_file_at(self, repo_path: Path, ref: str, file_path: str) -> bytes:
        """指定コミット時点のファイル内容を返す"""

    @abstractmethod
    def find_fix_commit(self, repo_path: Path, issue_keywords: list[str]) -> str | None:
        """コミットメッセージからIssue番号等でfix commitを探す"""

    @abstractmethod
    def find_buggy_commit(self, repo_path: Path, fix_ref: str, file_paths: list[str]) -> str:
        """fix commitの直前コミット（バグ混入状態）を返す"""
```

### 2.1 CommitInfo モデル

```python
@dataclass
class CommitInfo:
    ref: str              # コミットhash / svn revision等
    message: str
    author: UserInfo
    committed_at: datetime
    parent_refs: list[str]
    changed_files: list[str]
```

### 2.2 DiffInfo モデル

```python
@dataclass
class DiffInfo:
    from_ref: str
    to_ref: str
    unified_diff: str     # unified diff テキスト
    changed_files: list[str]
    lines_added: int
    lines_deleted: int
```

### 2.3 組み込み実装

| クラス | ファイル | 対応VCS |
|---|---|---|
| `GitAdapter` | `adapters/vcs/git.py` | Git（libgit2 / subprocess） |

---

## 3. IssueTrackerAdapter

```python
# bug-prospector/adapters/tracker/base.py
from abc import ABC, abstractmethod
from ..models.issue import IssueInfo, IssueQuery

class IssueTrackerAdapter(ABC):

    @property
    @abstractmethod
    def tracker_type(self) -> str:
        """アダプタが対応するトラッカー種別 (例: "github", "jira")"""

    @abstractmethod
    def search_bugs(self, query: IssueQuery) -> list[IssueInfo]:
        """バグ種別Issueを検索して返す。Rate Limitはアダプタ内で吸収する"""

    @abstractmethod
    def get_issue(self, issue_id: str) -> IssueInfo:
        """Issue詳細を1件取得する"""

    @abstractmethod
    def get_comments(self, issue_id: str) -> list[CommentInfo]:
        """Issueのコメントを取得する"""

    @abstractmethod
    def get_linked_refs(self, issue_id: str) -> list[str]:
        """Issueに紐付くコミットref / PRのブランチ名一覧を返す"""
```

### 3.1 IssueQuery モデル

```python
@dataclass
class IssueQuery:
    repo_url: str
    labels: list[str] = field(default_factory=lambda: ["bug"])
    state: str = "closed"        # "open" / "closed" / "all"
    since: datetime | None = None
    until: datetime | None = None
    limit: int | None = None
    extra: dict = field(default_factory=dict)  # トラッカー固有パラメータ
```

### 3.2 組み込み実装

| クラス | ファイル | 対応トラッカー |
|---|---|---|
| `GitHubAdapter` | `adapters/tracker/github.py` | GitHub Issues / REST API v3 |
| `GitLabAdapter` | `adapters/tracker/gitlab.py` | GitLab Issues / REST API v4 |

---

## 4. LanguageAdapter

```python
# bug-prospector/adapters/language/base.py
from abc import ABC, abstractmethod
from pathlib import Path
from ..models.environment import EnvironmentInfo

class LanguageAdapter(ABC):

    @property
    @abstractmethod
    def language_name(self) -> str:
        """言語名（小文字）。例: "python", "java", "javascript" """

    @abstractmethod
    def detect(self, repo_path: Path) -> bool:
        """このリポジトリが対象言語で書かれているか判定する"""

    @abstractmethod
    def get_language_version(self, repo_path: Path, ref: str) -> str | None:
        """指定コミット時点の言語バージョン要件を返す（例: ".python-version"読み取り）"""

    @abstractmethod
    def get_dependencies(self, repo_path: Path, ref: str) -> list[DependencyInfo]:
        """指定コミット時点の依存パッケージ一覧を返す"""

    @abstractmethod
    def get_build_commands(self, repo_path: Path) -> list[str]:
        """ビルドに使うコマンド列を返す（例: ["pip install -e ."]）"""

    @abstractmethod
    def get_test_commands(self, repo_path: Path) -> list[str]:
        """テスト実行コマンド列を返す（例: ["pytest"]）"""

    def build_environment_info(self, repo_path: Path, ref: str) -> EnvironmentInfo:
        """テンプレートメソッド。get_* を組み合わせてEnvironmentInfoを構築する"""
        return EnvironmentInfo(
            language=self.language_name,
            language_version=self.get_language_version(repo_path, ref),
            dependencies=self.get_dependencies(repo_path, ref),
            build_commands=self.get_build_commands(repo_path),
            test_commands=self.get_test_commands(repo_path),
        )
```

### 4.1 組み込み実装

| クラス | ファイル | 対応言語 |
|---|---|---|
| `PythonAdapter` | `adapters/language/python.py` | Python（pyproject.toml / requirements.txt / setup.py） |
| `JavaAdapter` | `adapters/language/java.py` | Java（pom.xml / build.gradle） |

---

## 5. AdapterRegistry

```python
# bug-prospector/core/registry.py
class AdapterRegistry:

    def register(self, category: str, name: str, cls: type) -> None:
        """アダプタを登録する"""

    def get_vcs(self, name: str) -> VCSAdapter:
        """名前でVCSアダプタインスタンスを取得する"""

    def get_tracker(self, name: str) -> IssueTrackerAdapter:
        """名前でIssueTrackerアダプタインスタンスを取得する"""

    def get_languages(self, names: list[str]) -> list[LanguageAdapter]:
        """名前リストから言語アダプタインスタンス一覧を取得する"""

    def load_entrypoints(self) -> None:
        """importlib.metadataでサードパーティアダプタを自動登録する"""
```

### 5.1 エントリポイント規約

サードパーティアダプタは `pyproject.toml` に以下のように記述して配布する。

```toml
[project.entry-points."defect_prospector.adapters"]
"vcs.svn"       = "my_package.adapters:SvnAdapter"
"tracker.jira"  = "my_package.adapters:JiraAdapter"
"language.rust" = "my_package.adapters:RustAdapter"
```

キーの形式は `{category}.{name}` とする。

## 6. エラー処理規約

アダプタ固有の例外は以下の基底クラスを継承すること。

```python
class AdapterError(Exception):
    """アダプタ共通の基底例外"""

class RateLimitError(AdapterError):
    """API Rate Limit超過。retry_after属性で再試行待機秒数を伝達する"""
    retry_after: int

class AuthenticationError(AdapterError):
    """認証失敗"""

class ResourceNotFoundError(AdapterError):
    """対象リソースが存在しない"""

class NetworkError(AdapterError):
    """ネットワークエラー（タイムアウト等）"""
```

コアの `Collector` はこれらを捕捉し、適切なリトライ・スキップ処理を行う。