# CLI仕様書 — defect-prospector

## 1. 概要

CLIツール名は `bp`（bug-prospector）とする。  
コマンド体系は `bp <command> [options]` の形式とし、`--help` を全コマンドで使えるようにする。

```
bp [--dataset-dir PATH] [--config PATH] [-v] <command> [...]
```

## 2. グローバルオプション

| オプション | 省略形 | デフォルト | 説明 |
|---|---|---|---|
| `--dataset-dir PATH` | | `./dataset` | データセットの保存ディレクトリ |
| `--config PATH` | `-c` | `./config.yaml` | 設定ファイルパス |
| `--verbose` | `-v` | off | 詳細ログを表示 |
| `--version` | | | バージョンを表示して終了 |

---

## 3. コマンド一覧

| コマンド | 概要 |
|---|---|
| `collect` | 指定プロジェクトからバグレコードを収集する |
| `list` | 収集済みバグレコードを一覧表示する |
| `info` | バグレコードの詳細を表示する |
| `checkout` | 指定バグのコード状態をチェックアウトする |
| `export` | データセットをファイルにエクスポートする |
| `config init` | 設定ファイルのテンプレートを生成する |

---

## 4. collect

バグレコードを収集する。既存レコードは上書きせずスキップする（べき等）。

```
bp collect [options]
```

### オプション

| オプション | 必須 | 説明 |
|---|---|---|
| `--tracker-type TYPE` | ★ | 課題管理アダプタ名（`github` / `gitlab` / `jira` ...） |
| `--tracker-url URL` | ★ | 課題管理システムのURL（GitHub: `https://github.com/owner/repo`） |
| `--repo-url URL` | | リポジトリURL（省略時はtrackerのURLから推定） |
| `--vcs-type TYPE` | | VCSアダプタ名（デフォルト: `git`） |
| `--query EXPR` | | 追加検索条件（トラッカー固有クエリ文字列） |
| `--labels LABEL,...` | | 絞り込むラベル（デフォルト: `bug`） |
| `--limit N` | | 最大収集件数 |
| `--since DATE` | | 指定日以降のIssueのみ（ISO 8601形式） |
| `--until DATE` | | 指定日以前のIssueのみ |
| `--dry-run` | | 実際の保存は行わず対象件数のみ表示 |

### 使用例

```bash
# GitHubリポジトリからバグ収集
bp collect \
  --tracker-type github \
  --tracker-url https://github.com/pallets/flask \
  --limit 500

# Jiraプロジェクトから収集（2023年以降）
bp collect \
  --tracker-type jira \
  --tracker-url https://issues.example.com/projects/PROJ \
  --repo-url https://github.com/example/proj \
  --since 2023-01-01

# 設定ファイルを使って収集
bp -c ./my-config.yaml collect
```

### 出力例

```
Collecting bugs from github (pallets/flask)...
  Issue #1234: [TypeError] render_template fails with None ... OK
  Issue #1235: already collected, skipping
  Issue #1236: fix commit not found, skipping
  ...
Collected: 48 new records (2 skipped, 1 failed)
```

---

## 5. list

収集済みバグレコードを一覧表示する。

```
bp list [options]
```

### オプション

| オプション | 説明 |
|---|---|
| `--format FORMAT` | 出力形式: `table`（デフォルト）/ `json` / `csv` |
| `--language LANG` | 言語で絞り込み |
| `--severity SEV` | 重要度で絞り込み（`critical` / `major` / `minor`） |
| `--tracker-type TYPE` | トラッカー種別で絞り込み |
| `--since DATE` | 収集日時で絞り込み（以降） |
| `--limit N` | 表示件数上限（デフォルト: 50） |

### 使用例

```bash
bp list
bp list --language python --severity critical
bp list --format json | jq '.[] | .id'
```

### 出力例（table形式）

```
ID                               TITLE                                  LANG    SEV       COLLECTED
github-pallets-flask-1234        TypeError in render_template ...        python  major     2024-11-01
github-pallets-flask-1236        Session cookie not set on redirect      python  minor     2024-11-01
...
Total: 48 records
```

---

## 6. info

バグレコードの詳細を表示する。

```
bp info <bug-id> [--format FORMAT]
```

### オプション

| オプション | 説明 |
|---|---|
| `--format FORMAT` | `text`（デフォルト）/ `json` |

### 使用例

```bash
bp info github-pallets-flask-1234
bp info github-pallets-flask-1234 --format json
```

### 出力例（text形式）

```
Bug ID:       github-pallets-flask-1234
Title:        TypeError in render_template when context is None
Tracker:      GitHub  https://github.com/pallets/flask/issues/1234
Severity:     major
Language:     Python 3.x
Reported:     2024-03-15  by user123
Closed:       2024-03-20

Repository:   https://github.com/pallets/flask
Buggy commit: a1b2c3d4
Fix commit:   e5f6a7b8
Changed files:
  - src/flask/templating.py

Fix summary:
  Check for None context before passing to Jinja2

Diff (first 20 lines):
  --- a/src/flask/templating.py
  +++ b/src/flask/templating.py
  @@ -42,6 +42,8 @@ def render_template(...):
  ...
```

---

## 7. checkout

指定したバグIDのコードをローカルにチェックアウトする。  
VCSのローカルクローンを利用し、指定コミットの状態でファイルを展開する。

```
bp checkout <bug-id> [options]
```

### オプション

| オプション | 省略形 | デフォルト | 説明 |
|---|---|---|---|
| `--state STATE` | `-s` | `buggy` | `buggy`（修正前）/ `fixed`（修正後） |
| `--output-dir PATH` | `-o` | `./<bug-id>` | チェックアウト先ディレクトリ |
| `--no-deps` | | off | 依存ファイル（pyproject.toml等）を含めない |

### 使用例

```bash
# バグ状態（修正前）をカレントディレクトリ配下にチェックアウト
bp checkout github-pallets-flask-1234

# 修正済み状態を指定ディレクトリにチェックアウト
bp checkout github-pallets-flask-1234 --state fixed --output-dir /tmp/flask-fixed

# 複数バグを連続チェックアウトしてビルド確認
for id in $(bp list --format json | jq -r '.[].id'); do
  bp checkout $id -o ./bugs/$id
done
```

### 出力例

```
Checking out github-pallets-flask-1234 (buggy state)...
  Ref: a1b2c3d4
  Fetching repository... done
  Extracting files... done
  Output: ./github-pallets-flask-1234/

Environment info:
  Language: Python
  Version:  >=3.8
  Install:  pip install -e .
  Test:     pytest tests/

To reproduce:
  cd ./github-pallets-flask-1234
  pip install -e .
  pytest tests/test_templating.py::test_render_none_context
```

### 動作仕様

1. `dataset/repos/<repo-slug>/` にローカルクローンが存在しない場合は自動クローンする
2. 一時的な detached HEAD を作成し、`--output-dir` にファイルをコピーする（クローンは汚染しない）
3. `--output-dir` が既に存在する場合はエラーを返す（`--force` で上書き可）

---

## 8. export

データセットをファイルにエクスポートする。

```
bp export [options]
```

### オプション

| オプション | デフォルト | 説明 |
|---|---|---|
| `--format FORMAT` | `jsonl` | `jsonl` / `csv` / `parquet` |
| `--output PATH` | `./dataset-export.<ext>` | 出力ファイルパス |
| `--language LANG` | | 言語で絞り込み |
| `--severity SEV` | | 重要度で絞り込み |
| `--include-diff` | off | diff本文をエクスポートに含める |
| `--include-comments` | off | Issueコメントをエクスポートに含める |

### 使用例

```bash
# Python バグを JSON Lines でエクスポート
bp export --language python --format jsonl --output python-bugs.jsonl

# 全データを Parquet でエクスポート（ML向け）
bp export --format parquet --include-diff --output dataset.parquet
```

---

## 9. config init

設定ファイルのテンプレートを生成する。

```
bp config init [--output PATH]
```

### 使用例

```bash
bp config init                        # ./config.yaml に生成
bp config init --output ~/my-bp.yaml  # 指定パスに生成
```

---

## 10. 終了コード

| コード | 意味 |
|---|---|
| 0 | 正常終了 |
| 1 | 一般エラー（引数不正等） |
| 2 | 設定ファイルエラー |
| 3 | 認証エラー |
| 4 | 収集エラー（一部失敗） |
| 10 | リソースが見つからない（`info` / `checkout`でバグIDが存在しない） |
