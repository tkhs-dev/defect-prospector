# RUGのどのアイデアを・どう実装したか

[RUG](rug-paper-summary.md)（Cheng et al., "Turbo LLM for Rust Unit Test Generation"）のうち、
**Rustのコンパイルエラー連鎖を防ぐための「ボトムアップ・逐次コンパイラ検証」という方法論だけ**を
`bp generate-tests`（[test-generation.md](test-generation.md)）のRust向け生成に取り込んだ。
採否の検討経緯は [rug-integration-note.md](rug-integration-note.md) を参照。本ファイルは
「結局どのアイデアを・どのコードで・どう実装したか」を実装者向けにまとめた技術メモ。

## 1. 採用したアイデア

RUGの核心は **Algorithm 1（BUILD_CONTEXT）**:

> テストに必要な型・値を一度に生成させるのではなく、型依存グラフをノード単位に分解し、
> **葉（プリミティブ型）から根（対象関数の引数）へ向かって**、各ノードごとに
> 「LLMに生成させる → コンパイラで検証する → 通ったものだけ次のノードへ渡す（キャッシュ・再利用）」
> を繰り返す。

これにより、「入力準備 → 関数呼び出し → アサーション」という長い推論チェーンのどこか一箇所の
誤りが全体を壊す**連鎖エラー**を、小さい単位での検証で早期に断ち切れる（論文の報告値:
ボトムアップ構築だけでカバレッジ+12.90%(GPT-3.5)/+4.94%(GPT-4)相当の寄与、ベースラインが
テストを作れなかったAPIの65.14%をカバー）。

採用したのはこの**方法論**であり、RUGの実装そのもの（型依存グラフのデータ構造、MIRコンパイラ
プラグイン、トレイト境界の積集合探索アルゴリズム等）ではない。

## 2. 採用しなかったアイデアと理由

| RUGの要素 | 採用しなかった | 理由 |
|---|---|---|
| 型依存グラフ構築（MIRコンパイラプラグイン、`nightly-2022-12-10`固定） | ○ | rustc内部APIに依存しツールチェーン陳腐化のリスクが大きい。対象プロジェクト（clap/serde/ripgrep等）は現行Rustで書かれており、解析用の古いnightlyとの不一致リスクがある。Rust専用の別言語サブシステムをPython製ツールに抱え込むコストも大きい |
| ファジングハーネス変換（bolero + libFuzzer） | ○ | libFuzzer系はLinux/macOS中心でWindows実行が不安定。対象クレートのCargo.tomlにfuzz依存を追加する操作がValidatorの「使い捨てworktreeに閉じた変更として必ず元に戻す」という設計前提と相性が悪い |
| コーパス選択（Algorithm 2、パス重み付きランキング） | ○ | 「同じ関数に対する多数の生成テストから代表を選ぶ」仕組みで、RUGの**クレート全体のカバレッジ最大化**という目的に紐づく。defect-prospectorは**特定の1バグに対し再現テスト1本を作る**問題であり、そもそもテストを複数生成して選抜する状況が発生しない |
| **ボトムアップ・逐次コンパイラ検証（Algorithm 1）** | **採用** | Rustのコンパイルエラー連鎖という、defect-prospectorでも実際に起きている問題に直接効く部分 |

詳しい議論は [rug-integration-note.md](rug-integration-note.md) の「決定事項」を参照。

## 3. どう実装したか

### 3.1 方針: 専用グラフではなくClaude Code自身に探索・検証させる

RUGはAlgorithm 1を実行するために、まず静的解析で型依存グラフを構築する。defect-prospectorでは
この**専用グラフ構築を作らず**、`claude -p`（エージェント型で自身のRead/Grep/Globツールを持つ）に
対し、**「ボトムアップに考え、各ステップを`cargo check`で検証してから次に進め」という方法論を
プロンプトの指示として与える**ことで代替した。型依存の解決・トレイト境界を満たす具体型の探索は
Claude Code自身の判断に委ねる。

これは実装コストとRustバージョン非依存という利点がある一方、RUGのような網羅的・決定的な
グラフ探索ではなく、Claude Codeの探索精度に依存する（6節「既知の限界」参照）。

### 3.2 コード対応

| RUGのAlgorithm 1のステップ | defect-prospectorでの対応 |
|---|---|
| 型依存グラフ `G=(V,E)` の構築 | なし（Claude Code自身がRead/Grep/Globで探索） |
| ノードを葉から根へ順に解決 | プロンプト指示 1〜2（`_RUST_BOTTOM_UP_ADDENDUM` 項目1〜2） |
| ノードごとにLLM生成→オラクルコンパイラで検証→キャッシュ | プロンプト指示 2（単体で`cargo check`し、通ったものを再利用せよという指示） |
| Trait/Proxyノード: 境界制約を満たす候補型の探索 | プロンプト指示 3（ブランケット実装の場合は推測せずクレート内を検索せよという指示） |
| 全引数が揃ったら最終テストを組み立てて再検証 | プロンプト指示 4 |
| 3回試しても解決しない場合は打ち切り、代替手段へ | プロンプト指示 5 |

実装は `bug_prospector/adapters/llm/claude_code.py` の2箇所のみ:

```python
# _RUST_BOTTOM_UP_ADDENDUM (62-90行目)
# task.language == "rust" のとき _TASK_PROMPT に追記する、上表のプロンプト指示本体

# ClaudeCodeAdapter.generate_test() (100-156行目) 内:
is_rust = task.language.lower() == "rust"
if is_rust:
    prompt += _RUST_BOTTOM_UP_ADDENDUM                    # 113-115行目
...
bash_patterns = [f"Bash({_test_command_prefix(task.test_command)} *)"]
if is_rust:
    bash_patterns.append("Bash(cargo *)")                 # 119-123行目
```

`Bash(cargo *)` の追加は、プロジェクトの`test_command`が`cargo test`以外（`cargo nextest run`や
ラッパー経由コマンド等）でも、ボトムアップ構築中の`cargo check`による自己検証が必ず使えるように
するため。`test_command`から単純に先頭トークンを取る`_test_command_prefix()`だけに頼ると、
`cargo`で始まらないコマンドの場合に`cargo check`がBash許可パターンから漏れてしまう。

この変更は**`ClaudeCodeAdapter.generate_test()`のプロンプト組み立てとBash許可パターンのみ**に
閉じている。`Validator.validate_with_generated_test()`（生成テストがfixコミットでPASS・
ソース差分のみ逆パッチしたbuggyコミットでFAILすることを確認する最終検証ループ）や
`TestGenerator`（候補レコードの選定・結果保存）は一切変更していない。RUGのAlgorithm 1は
あくまで「1回の`generate_test()`呼び出しの中で、より通りやすいコードを書かせるための工夫」であり、
生成されたテストが実際にバグを再現するかどうかの検証は、既存の仕組みがそのまま担う。

### 3.3 Rust以外の言語への影響

`task.language.lower() == "rust"` が偽のとき（`python`/`java`）、プロンプト追記も
`Bash(cargo *)` 追加も発生しない。Python/Javaの生成フローは本変更の前後で完全に同一。

## 4. 動作イメージ

Issueが「空文字列を渡すとpanicする」というバグで、対象関数が
`fn encode<E: Encoder>(&self, encoder: E) -> Result<EncodeError>` のようなトレイト境界付き
ジェネリクスを持つ場合（[rug-paper-summary.md](rug-paper-summary.md) の動機付け例と同種のケース）、
Claude Codeは次のように振る舞うことを期待する（プロンプトで指示している手順そのもの）:

1. `E: Encoder` を満たす具体型（例: `EncoderImpl`）をリポジトリ内でRead/Grepして探す。
2. `EncoderImpl` の構築にさらに `Writer`/`Config` が必要と分かったら、まず `Writer` の具体型
   （例: `SliceWriter`）だけを単体で書いて `cargo check` し、通ったら次へ進む。
3. `Config` がブランケット実装 `impl<T> Config for T where T: R1+R2+R3` しか無い場合、
   `R1+R2+R3` を満たす既存の型（例: `Configuration`）をクレート内から探す。
4. 動作確認が取れた断片を組み合わせて最終的な再現テストを1本組み立て、`cargo check`で再確認する。
5. どこかの断片が数回試しても通らなければ、より単純な構築（デフォルト実装等）にフォールバックする。

この後、`Validator.validate_with_generated_test()` が実際にfixコミットでPASS・buggyコミットで
FAILすることを確認し、両方満たして初めて `validation.status: llm_valid` として採用される。

## 5. テスト

`tests/adapters/test_claude_code.py`（`TestCommandConstruction`クラス）で以下を確認している。

- `language="rust"` のタスクでは、プロンプトに `"build bottom-up, verify as you go"` が含まれ、
  `--allowedTools` に `Bash(cargo *)` が追加される（`test_command` が `cargo` で始まらない
  `"just test"` のようなラッパー経由コマンドでも追加されることを確認）。
- `language="python"` のタスクでは、上記のいずれも含まれない（既存の挙動が変わっていないことの
  回帰確認）。

`Validator.validate_with_generated_test()` 自体のfix:PASS/buggy:FAIL判定ロジックは
`tests/core/test_validator_generated_test.py` で別途検証済み（本変更では未変更）。

実際に`claude -p`を呼び出してRustの複雑な型を解決させる一連の流れ（本番相当のコストがかかる）は
自動テストの対象外。小さめの`--limit`での実地確認を推奨する（[README.md](../README.md) の
`bp generate-tests`節参照）。

## 6. 既知の限界（RUG本来の手法との差異）

- **網羅性・決定性の欠如**: RUGの型依存グラフは静的解析により**決定的かつ網羅的**に型依存を
  たどる。本実装はClaude Code自身の探索に委ねているため、RUGの動機付け例のような「コードベース
  全体を探さないと正しい具体型にたどり着けない」ケースで、探索を諦めたり誤った型を推測する
  リスクがRUGの専用グラフより高い。
- **キャッシュ・再利用の弱さ**: RUGは検証済みの部分解をノード単位で明示的にキャッシュし、
  複数回の生成試行をまたいで再利用する。本実装では`validate_with_generated_test()`が試行ごとに
  `cargo check`等の中間生成物も含めてworktreeを`reset --hard`+`clean -fd`するため、1回の
  `generate_test()`呼び出しの中でのみ（Claude Code自身の文脈の中でのみ）断片の再利用が効き、
  試行をまたいだ再利用は無い。
- **候補の曖昧性解消**: RUGは複数候補があるとき「ローカルクレート内を優先」等の明示的な戦略
  （LS/RR/UF）を持つ（論文RQ5）。本実装はそのような明示的な優先順位付けをプロンプトでは
  指示しておらず、Claude Codeの判断に委ねている。
- 以上の結果、効果の程度はRUGの論文報告値（コンパイル成功率寄与+12.90%/+4.94%相当）と
  同等とは限らない。定量評価は行っていない。

## 7. 参照

- [rug-paper-summary.md](rug-paper-summary.md): RUG論文の要約
- [rug-integration-note.md](rug-integration-note.md): 採否・スコープの検討経緯と決定事項
- [test-generation.md](test-generation.md) 6.1.2節: `bp generate-tests`全体設計の中での位置づけ
- `bug_prospector/adapters/llm/claude_code.py`: 実装本体
- `tests/adapters/test_claude_code.py`: テスト
