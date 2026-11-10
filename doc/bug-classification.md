# 収集時のLLMバグ判定（`llm.classify_bugs`）

## 背景

Issue起点の収集は `search.labels`（例: `bug`）で Issue を絞り込んでいる。しかしプロジェクトによっては
ラベルでバグを管理しなくなっている。serde は2019年以降、closed Issue の約9割にラベルが無い
（2018年までは全Issueにラベルがあった）。ラベルで絞るとそうした近年のバグ報告を取りこぼし、
絞らないと機能要望・質問・リリース依頼等が大量に混ざる。

そこで「ラベルでは絞らず、修正（PR/コミット）を特定できた Issue について LLM にバグか判定させる」
という収集方法を用意する。

## 設定

```yaml
search:
  labels: []          # 空ならラベルで絞り込まない
  limit: null         # 上限なし
llm:
  provider: claude_code
  classify_bugs: true # llm.enabled（bp generate-tests 用）とは独立
```

- `search.labels` を省略した場合は従来どおり `[bug]`。空リストを明示したときだけ絞り込みを外す。
- GitHub検索APIは1クエリあたり最大1000件しか返さないため、超える場合は作成日の期間を二分割して
  検索し直す（`GitHubAdapter._search_by_date`）。結果は作成日の新しい順。

## 処理の流れ（`Collector._build_record`）

1. Issue に紐付く修正を特定する（Timeline の cross-reference のうち、`Fixes #N` 等で修正を明言している
   マージ済みPRを優先。無ければクローズしたコミット）。特定できなければスキップ（従来どおり）
2. buggy/fix の diff を取得する
3. **LLMバグ判定**: `LLMAdapter.classify_bug(task)` に Issue のタイトル・本文、修正のタイトル
   （PRタイトル。コミットで閉じられた場合はコミットメッセージの1行目）、ソース差分（テスト差分を除く）を渡す
4. バグなら従来どおりテスト検証する。バグでなければテスト検証を行わず、`validation.status = not_bug`
   で保存する
5. 判定結果は `BugRecord.bug_classification`（`is_bug` / `category` / `reason` / `provider` / `model` /
   `assessed_at`）に保存する

`category` は `bug` / `feature_request` / `question` / `documentation` / `maintenance` /
`unrelated_fix`（Issue はバグだが、特定した変更がその修正ではない）/ `other`。

## 振る舞いの詳細

- **バグでない Issue も保存する**: `not_bug` として保存するので、次回の `bp collect` では
  `Storage.exists()` によりスキップされ、再判定のコストがかからない。判定理由も後から確認できる。
  データセットから除く場合は `bp delete --tracker <url> --status not_bug`。
- **判定の失敗**: タイムアウト等で判定できなかった Issue は `failed` として数え、保存しない
  （次回の収集で再判定される）。
- **dry-run**: diff を取得しないので判定も行わない（LLMのコストもかからない）。
- **後段との関係**: `not_bug` は `bp generate-tests` の対象外（再挑戦対象のステータスに含まれない）。
  `bp generate-tests` の生成可否判定（`llm.screening`、doc/test-generation.md 3.1節）は
  「バグだがテストで再現できるか」を判定するもので、本判定の後段に位置する。
- **Claude Code 実装**: `claude -p --tools "" --json-schema ...` でツールを与えずに構造化出力で判定させる
  （生成可否判定と同じ `ClaudeCodeAdapter._judge`）。使用量は `<dataset-dir>/llm-usage/<project_id>.jsonl`
  に `kind: "classify"` として記録され、`bp collect` の終了時に呼び出し回数と合計コストが表示される。
- **`bp stats`**: `[LLM bug classification]` に判定結果の内訳を表示する。

## 既知の制約

- ラベル付きで既に収集済みのレコードは再判定しない（`bp collect` は既存レコードをスキップするため）。
  判定し直す場合は `bp delete` で削除してから再収集する。
- Issue を経由しない修正（PRのみ・直接コミット）は対象外。doc/pr-collection.md で別途設計中。
