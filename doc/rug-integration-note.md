# RUG統合の検討メモ（決定済み・実装済み）

[rug-paper-summary.md](rug-paper-summary.md)（RUG論文の要約）を、Rust向けの `bp generate-tests`
（[test-generation.md](test-generation.md) 6.1.2節）に統合した経緯の記録。

## 決定事項（2026-10-08）

以下の3点についてユーザーの了承を得て実装済み。

1. **スコープ**: 「核心アイデアのみ統合」案を採用（フル再現・独立スタンドアロン実装は見送り）。
2. **静的解析の代替**: (a) Claude Code自身に`-p`でRead/Grep/Globと`cargo check`を使わせて探索・
   自己検証させる方式を採用（syn等による軽量パーサ補助(b)は不採用）。
3. **対象言語**: Rustのみ。RUGはRust固有の問題（トレイト境界・ジェネリクス）を解決する手法のため、
   Java/Pythonには適用しない。

実装は `bug_prospector/adapters/llm/claude_code.py` の `_RUST_BOTTOM_UP_ADDENDUM`
（`TestGenerationTask.language == "rust"` の場合にプロンプトへ追記）と、Rust向けの
`Bash(cargo *)` 許可パターン追加のみ。`Validator.validate_with_generated_test()` 側
（fix:PASS / buggy:FAIL の最終確認ループ）は変更していない。詳細は
[test-generation.md](test-generation.md) 6.1.2節を参照。

## 経緯

ユーザーから「RUGを再現してほしい。LLMはClaude Codeの`-p`を使うこと」という依頼があった。
論文の規模（静的解析MIRコンパイラプラグイン＋LLMボトムアップ生成＋ファジングハーネス変換＋
コーパス選択アルゴリズムの4本柱）を把握した上で、フル再現には大きな問題があることを
ユーザーに説明し、スコープの合意を取ろうとしている段階。AskUserQuestionでの選択肢提示は
一度拒否されたため、チャットでの平文のやり取りで論点を詰めている。

## 論点（ユーザーに提示済みの内容）

### 1. RUGとdefect-prospectorは目的が違う

- RUG: クレート内の**できるだけ多くの関数**に対しユニットテストを量産し、**カバレッジを最大化**する。
- defect-prospector: **特定の1バグ**に対し、それを再現するテストを**1本**作る
  （Issue本文・fix diffというオラクル・手がかりが既にある、はるかに狭い問題）。
- ファジング（Algorithm 2、コーパス選択含む）はRUGの「広く浅く」という目的に強く結びついており、
  defect-prospectorの「狭く深く」という目的への寄与は相対的に小さいと判断している。

### 2. フル再現（静的解析プラグイン＋ファジング）の具体的な問題点（ユーザーに説明済み）

1. rustc内部API（MIR）に依存する独自コンパイラプラグインは`nightly-2022-12-10`固定。rustc内部APIは
   非安定でバージョンごとに壊れる。対象プロジェクト（clap/serde/ripgrep等）は現行Rustで書かれており、
   解析用の古いnightlyとビルド用の現行ツールチェーンの二重管理が必要になりかねない。
2. bug_prospectorはPython製。rustcプラグインはRustで書く必要があり、bolero+libFuzzerはCargo+LLVM
   SanitizerCoverageに依存。別言語・別ビルドシステムを抱え込むことになる。
3. libFuzzer/cargo-fuzzはLinux/macOS中心で、このユーザーの実行環境（Windows）でのネイティブ動作は
   不安定。WSL前提にするなら実行要件自体を変える必要がある。
4. ファジングハーネス変換は対象クレートのCargo.tomlにbolero依存を追加する等の操作を伴い、
   Validatorの「使い捨てworktreeに閉じた変更として必ず`reset --hard`+`clean -fd`で元に戻す」という
   既存の設計前提と相性が悪い。
5. RUGの評価数値（GPT-4で71.37%等）はGPT-3.5/4・特定パラメータに依存しており、Claude Codeを使う
   以上フル実装しても論文の数値そのものは再現できない（あくまで手法の移植）。
6. 原著者ら自身、Python本体＋Rustプラグインの2言語体制で数ヶ月規模の研究プロジェクトとして実装して
   おり、個人/小規模運用のbug_prospectorでこの規模のサブシステムを維持するのは明らかに不釣り合い。

## 現時点での推奨（ユーザーへの提案、まだ合意は得ていない）

**核心アイデアのみをdefect-prospectorに統合する案**を推奨として提示した:

- RUGの価値の核心は「LLMに大きな問題を一度に解かせず、型依存グラフでノード単位に分割し、
  **各ステップをコンパイラで検証してから積み上げる**（Algorithm 1 / BUILD_CONTEXT）」という考え方。
  この部分だけでコンパイル成功率に+12.90%(GPT-3.5)/+4.94%(GPT-4)相当の寄与があったと論文は報告している。
- 専用の型依存グラフ構築（MIRプラグイン）は作らず、代わりに **Claude Code自身に`-p`で
  Read/Grep/Globを使わせてリポジトリを探索させ、依存する型・トレイト実装・コンストラクタを
  ボトムアップに見つけさせる**方針が有力候補（静的解析の代替案としてユーザーに提示済み、
  まだ選択されていない）。もう一つの代替案として「syn等による軽量な依存グラフ構築をPythonで補助する」
  も提示した。
- ファジングハーネス変換＋コーパス選択（Algorithm 2）は、defect-prospectorの目的
  （特定バグの再現、既にオラクルがある）への寄与が相対的に小さいと判断し、**スコープ外**とする方向。
- 実装の置き場所としては、既存の `bug_prospector/adapters/llm/` ／ `core/validator.py` の
  `LLMAdapter` / `validate_with_generated_test` の枠組みをそのまま使い、Rust向けにボトムアップ生成
  ステップを挟む拡張、というのが自然な統合点になりそう（まだ設計はしていない）。

## 関連ファイル

- [test-generation.md](test-generation.md): 既存の `bp generate-tests` 設計書（実装済み、
  commit `60809a7` 設計更新 / `c7780a5` 実装）。RUG統合はこの上に乗せる想定。
- [rug-paper-summary.md](rug-paper-summary.md): RUG論文の要約（このメモの元ネタ）。
- 元PDF: `C:\Users\daiki\claude-tmp\rug\cheng_rug.pdf`（このリポジトリには含まれていない、
  パスワード保護されており本文は未読。要約markdownのみ参照できた）。別PCでは参照できない点に注意。
