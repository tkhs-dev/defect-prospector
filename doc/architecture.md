# アーキテクチャ設計書 — defect-prospector

## 1. システム全体構成

```
defect-prospector/
├── bug-prospector/               # メインパッケージ
│   ├── adapters/
│   │   ├── vcs/                  # VCSアダプタ群
│   │   │   ├── base.py           #   抽象基底クラス
│   │   │   ├── git.py            #   Git実装
│   │   │   └── ...
│   │   ├── tracker/              # 課題管理アダプタ群
│   │   │   ├── base.py
│   │   │   ├── github.py
│   │   │   ├── gitlab.py
│   │   │   └── ...
│   │   └── language/             # 言語アダプタ群
│   │       ├── base.py
│   │       ├── python.py
│   │       └── ...
│   ├── cli/                      # CLIエントリポイント
│   │   ├── main.py
│   │   └── commands/
│   │       ├── collect.py
│   │       ├── checkout.py
│   │       ├── list_.py
│   │       ├── info.py
│   │       └── export.py
│   ├── core/
│   │   ├── models/               # データモデル定義
│   │   │   ├── record.py         #   BugRecord（中心モデル）
│   │   │   ├── issue.py          #   IssueInfo
│   │   │   ├── commit.py         #   CommitInfo
│   │   │   └── environment.py    #   EnvironmentInfo
│   │   ├── collector.py          # 収集オーケストレーター
│   │   ├── storage.py            # データセット永続化
│   │   └── registry.py           # アダプタレジストリ
│   └── config.py                 # 設定読み込み
├── doc/                          # 設計書
└── pyproject.toml
```

## 2. レイヤー構成

```
┌─────────────────────────────────────┐
│              CLI Layer              │  コマンド解析・ユーザー出力
└────────────────┬────────────────────┘
                 │
┌────────────────▼────────────────────┐
│             Core Layer              │  収集オーケストレーション・モデル・ストレージ
│  Collector  │  Models  │  Storage   │
└──────┬──────────────────────────────┘
       │ インターフェース経由のみ
┌──────▼──────────────────────────────┐
│           Adapter Layer             │
│  VCS  │  IssueTracker  │  Language  │
└──────────────────────────────────────┘
       │               │
  外部VCS         外部Issue Tracker
  (Git/SVN/...)   (GitHub/Jira/...)
```

- **Core はアダプタ実装クラスを直接参照しない**。常にインターフェース（ABC）経由でアクセスする。
- アダプタは `AdapterRegistry` に登録し、設定ファイルの `type` キーで選択する。

## 3. コンポーネント詳細

### 3.1 Collector（収集オーケストレーター）

収集処理の主制御クラス。以下のフローを実行する。

```
Collector.run(config)
  1. IssueTrackerAdapter.search_bugs(query)
       → List[IssueInfo]
  2. for each issue:
       a. IssueTrackerAdapter.get_linked_commits(issue) → fix_commit
       b. VCSAdapter.find_buggy_commit(fix_commit)      → buggy_commit
       c. VCSAdapter.get_diff(buggy_commit, fix_commit) → diff
       d. VCSAdapter.get_commit_info(fix_commit)        → CommitInfo
       e. LanguageAdapter.detect(repo_path)             → EnvironmentInfo
       f. build BugRecord
  3. Storage.save(bug_record)
```

収集はべき等に設計する（同一 Issue を二重収集しない）。

### 3.2 AdapterRegistry

```python
# 登録
registry.register("vcs", "git", GitAdapter)
registry.register("tracker", "github", GitHubAdapter)

# 取得（設定から）
vcs_adapter: VCSAdapter = registry.get("vcs", config.vcs.type)
```

組み込みアダプタは起動時に自動登録される。サードパーティアダプタはエントリポイント（`pyproject.toml` の `[project.entry-points]`）で登録できる。

### 3.3 Storage

- デフォルトは SQLite ベース（`dataset.db`）+ ファイルシステム（差分・ソースファイル）
- `Storage` も抽象化し、将来的な PostgreSQL 等への切り替えを可能にする
- VCSのローカルクローンは `<dataset-dir>/repos/<repo-id>/` に保管

### 3.4 設定ファイル構造（`config.yaml`）

```yaml
dataset_dir: ./dataset

vcs:
  type: git                      # 使用するVCSアダプタ名
  auth:
    ssh_key: ~/.ssh/id_rsa       # オプション

tracker:
  type: github
  url: https://api.github.com
  auth:
    token: ${GITHUB_TOKEN}       # 環境変数参照

language:
  - python                       # 使用する言語アダプタ（複数可）
  - java

collect:
  query: "label:bug state:closed"
  limit: 1000
  since: "2020-01-01"
```

## 4. アダプタプラグイン機構

サードパーティがアダプタを配布できるよう、エントリポイントで登録する。

```toml
# サードパーティアダプタの pyproject.toml
[project.entry-points."defect_prospector.adapters"]
"vcs.svn"      = "my_package.adapters.svn:SvnAdapter"
"tracker.jira" = "my_package.adapters.jira:JiraAdapter"
```

ツール起動時に `importlib.metadata.entry_points()` で自動検出・登録される。

## 5. データフロー図

```
GitHub/GitLab/Jira                  Git/SVN
      │                                │
      ▼                                ▼
IssueTrackerAdapter           VCSAdapter
      │                                │
      └──────────┬─────────────────────┘
                 ▼
            Collector
          (BugRecord組み立て)
                 │
          LanguageAdapter
          (環境情報付加)
                 │
                 ▼
            Storage
         (SQLite + Files)
                 │
         ┌───────┼────────┐
         ▼       ▼        ▼
       CLI    Export    Checkout
```
