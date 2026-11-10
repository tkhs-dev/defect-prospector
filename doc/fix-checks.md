# 修正の機械的な確認（`fix_checks`）

## 目的

収集した「修正」が正しい修正であることは、どの収集元でも保証できない。テストの FAIL→PASS
（`valid` / `llm_valid`）が示すのは「その変更でテストが観測する挙動が変わった」ことまでで、
修正後の挙動が正しいかは別の根拠に頼っている。

| 根拠 | Issue起点 | PR起点 | 直接コミット |
|---|---|---|---|
| 修正者以外による独立したバグ報告 | あり | なし | なし |
| レビュー・議論の記録 | PR経由ならあり | あり | なし |
| 修正内容の説明 | Issue＋PR | PR本文 | コミットメッセージのみ |

そこで、事後の履歴から「疑わしい修正」に機械的に印を付ける。どれも保証ではないが、誤った修正や
不完全な修正を減らせる。

## 確認の内容（`bug_prospector/core/fix_checks.py`）

| 名前 | 落ちる条件（passed=False） | 判定不能（passed=None） |
|---|---|---|
| `revert` | デフォルトブランチ上の後続コミットが、修正を構成するいずれかのコミットを `This reverts commit <sha>` で参照している、または件名が `Revert "<修正の件名>"` | — |
| `follow_up_fix` | 修正で追加したソース行（テスト以外）のうち、修正から `follow_up_window_days` 日以内に書き換え・削除されたものがある（`git blame --reverse`）。「修正の修正」の疑い | — |
| `release` | 修正コミットを含むタグが無い（未リリース） | — |
| `ci` | PRならPRのhead（マージ前）、それ以外は修正コミットの check-runs / commit status に失敗がある | CIの結果が無い・実行中のみ |

確認の実行自体が失敗した場合（gitコマンドのエラー等）は passed=None として記録する。

注意:
- `release` はリリース前の新しい修正をすべて落とす。`exclude` にする場合は収集期間（`search.until`）と
  合わせて使う。
- `follow_up_fix` は無関係なリファクタリングで同じ行が書き換えられた場合にも落ちる。詳細
  （書き換えたコミットの件名）は `data.follow_up_commits` と `detail` に残るので、`record` で集めて
  傾向を見てから `exclude` にするか決めるのがよい。
- `ci` でPRのマージ後のコミットを見ないのは、マージ後のCIが後から nightly の新しい lint 等で落ちることがあり
  修正の良し悪しと関係しないため（serde #3034: head=success / マージコミット=failure）。
- `ci` は GitHub API を1レコードあたり2回呼ぶ。2016年頃の Travis CI 等、記録が残っていないものは
  判定不能になる。

## 設定

各確認は次の3つのモードから選ぶ。

- `off`: 実行しない（既定）
- `record`: 実行して結果を `BugRecord.fix_checks` に記録するだけ
- `exclude`: 記録したうえで、passed=False なら `validation.status = fix_rejected` として保存し、
  LLMバグ判定・テスト検証を行わない（判定不能の passed=None では除外しない）

トップレベルの `fix_checks` が全収集元の既定値で、`sources.<収集元>.fix_checks` で収集元ごとに
キー単位で上書きできる。

```yaml
fix_checks:                 # 既定値
  revert: record
  follow_up_fix: record
  follow_up_window_days: 30
  release: record
  ci: record

sources:
  issues:
    enabled: true           # Issue起点はそのまま記録だけ
  pull_requests:
    enabled: true
    fix_checks:
      revert: exclude       # PR起点はリバートされたものを除外
  commits:
    enabled: true
    fix_checks:             # 裏付けの少ない直接コミットは厳しめに
      revert: exclude
      follow_up_fix: exclude
```

`true` / `false` は `record` / `off` として扱う。

## 結果の確認

- `bp stats` の `[Fix checks]` に確認ごとの pass / fail / unknown の件数を表示する
- `bp stats` の `[Validation status by source kind]` で、出自（issue / pull_request / commit）ごとの
  検証ステータスを比較できる。独立したバグ報告が無い pull_request / commit の `llm_valid` は、
  生成テストが修正後の挙動を写しただけの可能性があり、Issue起点の `llm_valid` より根拠が弱い
- `bp generate-tests --kind issue` のように、出自を絞ってテスト生成できる
- 除外したレコードを消すには `bp delete --tracker <url> --status fix_rejected`
