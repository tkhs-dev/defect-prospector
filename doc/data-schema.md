# データスキーマ定義書 — defect-prospector

## 1. 概要

1件のバグを表す最上位モデルは `BugRecord` とする。  
各フィールドは収集源を問わない共通表現であり、アダプタが各システム固有のデータをこの形式にマッピングする。

## 2. BugRecord（中心モデル）

```
BugRecord
├── id                  str          データセット内一意ID（例: "GH-flask-1234"）
├── source              IssueInfo    課題管理システムから取得した情報
├── repository          RepoInfo     VCSリポジトリ情報
├── reproduction        ReproInfo    再現に必要な情報
├── fix                 FixInfo      修正内容
└── metadata            MetaInfo     収集メタデータ
```

## 3. 各モデル定義

### 3.1 IssueInfo

| フィールド | 型 | 説明 |
|---|---|---|
| `tracker_type` | str | トラッカー種別 (`github` / `gitlab` / `jira` / `bugzilla`) |
| `tracker_url` | str | トラッカーのベースURL |
| `issue_id` | str | トラッカー内でのIssue ID |
| `issue_url` | str | IssueへのURL |
| `title` | str | Issueタイトル |
| `body` | str | Issue本文 |
| `labels` | list[str] | ラベル・タグ一覧 |
| `severity` | str \| None | 重要度（`critical`/`major`/`minor`/`trivial`） |
| `reporter` | UserInfo | 報告者情報 |
| `assignees` | list[UserInfo] | 担当者一覧 |
| `created_at` | datetime | Issue作成日時（UTC） |
| `closed_at` | datetime \| None | Issue解決日時（UTC） |
| `linked_prs` | list[str] | 関連するPR/MR URL一覧 |

### 3.2 UserInfo

| フィールド | 型 | 説明 |
|---|---|---|
| `login` | str | ユーザーID / ログイン名 |
| `display_name` | str \| None | 表示名 |
| `url` | str \| None | プロフィールURL |

### 3.3 RepoInfo

| フィールド | 型 | 説明 |
|---|---|---|
| `vcs_type` | str | VCS種別（`git` / `svn` / `hg`） |
| `url` | str | リポジトリURL |
| `buggy_commit` | str | バグが存在するコミットのref（ハッシュ/リビジョン） |
| `fix_commit` | str | バグを修正したコミットのref |
| `buggy_files` | list[str] | 修正コミットで変更されたファイルパス一覧 |
| `module_path` | str \| None | モノリポの場合のサブモジュールパス |

### 3.4 ReproInfo

再現するために必要な情報を集約する。

| フィールド | 型 | 説明 |
|---|---|---|
| `steps` | list[str] \| None | 再現手順（Issue本文から抽出） |
| `expected_behavior` | str \| None | 期待される動作 |
| `actual_behavior` | str \| None | 実際の動作 |
| `environment` | EnvironmentInfo | 実行環境情報 |
| `test_cases` | list[TestCaseInfo] | 関連テストケース一覧 |

### 3.5 EnvironmentInfo

| フィールド | 型 | 説明 |
|---|---|---|
| `language` | str | プログラミング言語（例: `python`） |
| `language_version` | str \| None | 言語バージョン（例: `3.11.4`） |
| `runtime` | str \| None | ランタイム名（JVM等） |
| `runtime_version` | str \| None | ランタイムバージョン |
| `dependencies` | list[DependencyInfo] | 依存パッケージ一覧 |
| `os_hint` | str \| None | OS情報（Issueから抽出された場合） |
| `build_commands` | list[str] | ビルドコマンド（言語アダプタが推定） |
| `test_commands` | list[str] | テスト実行コマンド（言語アダプタが推定） |

### 3.6 DependencyInfo

| フィールド | 型 | 説明 |
|---|---|---|
| `name` | str | パッケージ名 |
| `version` | str \| None | バージョン指定文字列 |
| `source` | str \| None | 取得元（`requirements.txt` 等） |

### 3.7 TestCaseInfo

| フィールド | 型 | 説明 |
|---|---|---|
| `file_path` | str | テストファイルのパス |
| `function_name` | str \| None | テスト関数・メソッド名 |
| `test_type` | str | テスト種別（`unit` / `integration` / `regression`） |

### 3.8 FixInfo

| フィールド | 型 | 説明 |
|---|---|---|
| `commit` | str | 修正コミットref |
| `message` | str | コミットメッセージ |
| `author` | UserInfo | コミット作者 |
| `committed_at` | datetime | コミット日時（UTC） |
| `diff` | str | 修正差分（unified diff形式） |
| `changed_files` | list[str] | 変更ファイル一覧 |
| `lines_added` | int | 追加行数 |
| `lines_deleted` | int | 削除行数 |

### 3.9 MetaInfo

| フィールド | 型 | 説明 |
|---|---|---|
| `collected_at` | datetime | 収集日時（UTC） |
| `collector_version` | str | ツールバージョン |
| `schema_version` | str | データスキーマバージョン（例: `1.0`） |
| `notes` | str \| None | 補足メモ |

## 4. バグID命名規則

```
{tracker_type}-{repo_slug}-{issue_id}
```

例:
- `github-pallets-flask-1234`
- `jira-PROJ-567`
- `gitlab-gnome-gimp-890`

`repo_slug` は リポジトリURLの `owner/name` 部分を `-` 区切りに変換したもの。

## 5. ストレージ形式

### 5.1 SQLite テーブル構成（概略）

```sql
-- バグレコード主テーブル（JSONカラムで柔軟に保持）
CREATE TABLE bug_records (
    id           TEXT PRIMARY KEY,
    project_url  TEXT NOT NULL,
    tracker_type TEXT NOT NULL,
    issue_id     TEXT NOT NULL,
    language     TEXT,
    severity     TEXT,
    collected_at TEXT NOT NULL,
    record_json  TEXT NOT NULL    -- BugRecord全体をJSON化
);

-- VCSローカルクローンの管理
CREATE TABLE repositories (
    url          TEXT PRIMARY KEY,
    local_path   TEXT NOT NULL,
    vcs_type     TEXT NOT NULL,
    last_fetched TEXT
);
```

### 5.2 ファイルシステム構成

```
dataset/
├── dataset.db                    # SQLite DB
├── repos/                        # ローカルクローン
│   └── {repo-slug}/              #   各リポジトリ
└── diffs/                        # 差分ファイル（大きい場合のみ）
    └── {bug-id}.diff
```

## 6. エクスポート形式

### JSON Lines（`.jsonl`）

1行1件の `BugRecord` JSON。ML処理に最適。

```json
{"id": "github-pallets-flask-1234", "source": {...}, "fix": {...}, ...}
{"id": "github-pallets-flask-1235", ...}
```

### CSV

フラット化した主要フィールドのみ。分析用途向け。

```
id,title,language,severity,buggy_commit,fix_commit,lines_added,lines_deleted,collected_at
```

### Parquet

全フィールドをネストしたスキーマで保存。大規模データ処理向け。