# PR起点・直接コミットのバグ収集

ステータス: 実装済み（`bug_prospector/core/collector.py` の `_collect_pull_requests` / `_collect_commits`、
補助関数は `bug_prospector/core/sources.py`）。

## 1. 背景と目的

Issue起点の収集は「closed Issue → それを参照するマージ済みPR（またはクローズしたコミット）」の順に修正を
特定する。しかし近年のRustプロジェクトでは、バグがIssueを経由せずにPR、あるいはPRすら経由せずに
デフォルトブランチへの直接コミットで修正されることが多い。

serde の実測（2026-11-10、`origin/master` の first-parent 履歴とGitHub検索）:

| 期間 | closed Issue | マージ済みPR | master への first-parent コミット |
|---|---|---|---|
| 2019年以降 | 約780（約9割がラベルなし） | 320 | マージコミット 313 / 直接コミット 740 |

マージ方式はプロジェクトによって異なる（2022年以降の first-parent 履歴）:
serde はPRを全てマージコミットで取り込む。ripgrep はマージコミットが0件（PRをリベースして取り込む）。

## 2. 収集元とオンオフ

`sources:` で収集元を個別にオンオフする。CLI の `bp collect --source <name>`（複数指定可）で上書きできる。

| 収集元 | `IssueInfo.kind` | bug_id | 既定 |
|---|---|---|---|
| `issues` | `issue` | `github-{owner}-{repo}-{N}` | 有効 |
| `pull_requests` | `pull_request` | `github-{owner}-{repo}-pr-{N}` | 無効 |
| `commits` | `commit` | `github-{owner}-{repo}-commit-{sha12}` | 無効 |

実行順は issues → pull_requests → commits で固定。後の収集元は、前の収集元（と既存レコード）が既に
取り込んだPR・修正コミットを除外する（5節）。

出自は `BugRecord.source.kind` に記録され、`bp stats` の `[Source kind]` / `[Validation status by source kind]`
で区別して集計できる。独立したバグ報告を持たない pull_request / commit は、修正の正しさの根拠が弱い
（doc/fix-checks.md）。

## 3. 全体フロー

```
PR起点（_collect_pull_requests）
  1. GitHub検索 is:pr is:merged merged:{since}..{until}（1000件を超えたら期間を二分割）
  2. 検索結果だけで判定できる除外（_pr_precheck）
       収集済み / Issue起点で収集済みのPR / タイトルのキーワード / bot作成 /
       本文で "Fixes #N" している Issue が収集済み
  3. PR詳細の取得（base・head・マージSHA・コミット件名）
       base がデフォルトブランチでない / マージSHAが収集済み → 除外
  4. マージ方式の判定と buggy コミットの特定（7節）
  5. diff取得とソース変更の事前フィルタ（6節）
  6. 共通の後段処理へ

直接コミット（_collect_commits）
  1. git log origin/<default> --first-parent --no-merges（search.since / search.until）
  2. 収集済み / 件名のキーワード / bot作成 → 除外
  3. buggy = parent、diff取得とソース変更の事前フィルタ（6節）
  4. exclude_pr_commits: GitHub API でPRに属すると分かったコミットを除外
     （squash/rebase で取り込まれたPRのコミット。事前フィルタを通ったものだけ1回ずつ呼ぶ）
  5. 共通の後段処理へ

共通の後段処理（_finish_record）
  a. 修正の機械的な確認（doc/fix-checks.md）。exclude で落ちたら fix_rejected
  b. llm.classify_bugs ならLLMバグ判定（8節）。バグでなければ not_bug
  c. テスト検証（Validator.validate）
```

`--dry-run` は 1〜3（直接コミットは 1〜4）まで実行し、保存・確認・判定・検証は行わない。直接コミットの
収集にはローカルクローンが必要なので、初回は dry-run 以外で実行する。

## 4. 設定

```yaml
search:                       # Issue起点の条件。since / until は全収集元で共通
  since: "2011-01-01"

sources:
  issues:
    enabled: true
  pull_requests:
    enabled: false
    labels: []                # PRのラベルで絞る場合
    title_keywords: null      # 例: [fix, bug, panic]。null なら絞らない
    exclude_bots: true
    max_source_lines: 2000    # ソース差分（テスト以外）の追加+削除行数の上限。null なら上限なし
    ignore_paths: [...]       # これだけを変更する修正は除外（既定: *.md, docs/**, .github/**, *.toml 等）
    limit: null
  commits:
    enabled: false
    title_keywords: null
    exclude_bots: true
    max_source_lines: 2000
    ignore_paths: [...]
    exclude_pr_commits: true
    limit: null
```

各収集元は `fix_checks:` で修正確認の既定値を上書きできる（doc/fix-checks.md）。

## 5. 重複除外

- 実行開始時に、同じトラッカーの既存レコードから修正コミットとPR番号を集め、実行中に保存したものも追加する
- PR起点: 既存PR番号・修正コミットと一致するもの、本文で `Fixes #N` 等と修正を明言している Issue が
  既に収集済みのものを除く（Issue起点の方が、独立したバグ報告という情報源を持つため優先）
- 直接コミット: 既存の修正コミットと一致するもの、PRに属するものを除く

## 6. ソース変更の事前フィルタ（LLM呼び出し前）

`sources.source_change_rejection`:

- テスト以外の変更が無い（逆パッチ検証ができない）
- テスト以外の変更が `ignore_paths` に当たるファイルだけ
- ソース差分の行数が `max_source_lines` を超える（機能追加・リファクタリングの可能性が高い）

タイトルのキーワード（`title_keywords`）は既定では使わない。使うとLLM判定の件数は大きく減るが
（serde 2019年以降のPR: 320件 → 40件）、`Allow derive(Deserialize) for generic unit structs`（#2500、
実際はバグ修正）のようなキーワードを含まない修正を落とす。

## 7. buggy / fix コミットの特定（`sources.resolve_pr_fix_range`）

| 方式 | 判定 | buggy | fix |
|---|---|---|---|
| マージコミット | `merge_commit_sha` の親が2つ | first parent | merge_commit |
| rebase | 親が1つ、PRのコミットが k(>1) 個で、first-parent を k 個遡った件名の集合がPRのコミット件名と一致 | merge_commit~k | merge_commit |
| squash（1コミットのPRを含む） | 上記以外 | parent | merge_commit |

判定結果は `PRInfo.merge_method` に記録する。

注: Issue起点の既存実装は常に「first parent を buggy」としており、rebase で取り込まれた複数コミットのPRでは
diff が最後のコミット分だけになる可能性がある（ripgrep 等）。Issue起点への適用は未対応。

## 8. LLMバグ判定

`llm.classify_bugs: true` のとき、既存の `LLMAdapter.classify_bug` を呼ぶ。`TestGenerationTask.source_kind` が
`pull_request` / `commit` のときは、「独立したバグ報告は無く、修正者自身の説明しかない」ことを明示した
専用プロンプト（`_FIX_ONLY_CLASSIFICATION_PROMPT`）を使い、Issue欄の代わりにPRのタイトル・本文、または
コミットメッセージを渡す。カテゴリから `question` / `unrelated_fix` は外れる。

PR・直接コミットはバグ以外の変更が大半なので、これらを有効にするときは `classify_bugs: true` か
`title_keywords` のどちらかを使うことを推奨する。

## 9. 既知の制約・今後

- PR起点の `get_pull_details` はPR1件につきAPIを2回呼ぶ（詳細とコミット一覧）。マージ済みPRが多い
  プロジェクトでは `title_keywords` や `limit` で絞る
- 直接コミットのうち、PRに属するかの判定は GitHub API の `commits/{sha}/pulls` に依存する
- Issue起点への rebase 対応（7節の注）は未対応
