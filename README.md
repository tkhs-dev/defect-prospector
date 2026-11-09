# defect-prospector

GitHub上のバグ修正PRを収集し、再現可能なバグのデータセットを構築するツール。
Rust / Java / Python のプロジェクトを対象に、

1. バグ修正PRから buggy / fix コミットと修正差分を集め、
2. リバースパッチで「buggy状態でFAIL・fix状態でPASSするテスト」があるかを検証し、
3. 修正にテストが同梱されていないバグには、LLMで再現テストを合成して同じ検証にかける

ことで、テストで再現が確認されたバグ（`valid` / `llm_valid`）を蓄積する。

設計時点のドキュメントは `doc/` にあるが、実装とはズレている部分が多い
（`doc/cli-spec.md` の「11. 実装済みの拡張コマンド」参照）。**本READMEが
現在の実装の実態に即した一次情報**。

## セットアップ

```bash
uv sync --extra dev
```

`.env` に GitHub Personal Access Token を置くと自動で読み込まれる（`bug_prospector/cli/main.py` が起動時に `load_dotenv()` を呼ぶ）。

```
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

言語ごとに、実際にテスト検証を行うためのツールチェインが別途必要:

| 言語 | 必要なツール |
|---|---|
| Rust | `cargo` |
| Java | JDK、Maven または Gradle（プロジェクトにより異なる。`gradlew` があればラッパー経由で実行） |
| Python | `pip`（各プロジェクトの `setup_commands` で `pytest` 等をインストール） |

`bp generate-tests`（LLMによる再現テスト生成）を使う場合は、[Claude Code CLI](https://docs.claude.com/claude-code)（`claude`）も必要。

## クイックスタート

```bash
# 1件も保存せずに、検索条件やラベルが正しいか確認する
bp collect --project clap.yaml --limit 3 --dry-run

# 実際に収集する（GitHub検索 → PR特定 → diff取得 → テスト検証 → 保存）
bp collect --project clap.yaml --limit 20

# テストが同梱されていないバグに、LLMで再現テストを生成する
bp generate-tests --project clap.yaml --limit 5

# 収集済みデータセットの統計を見る
bp stats
```

`--dataset-dir`（デフォルト `./dataset`）でデータの保存先を切り替えられる。実験用に
本番データと分けたいときは `bp --dataset-dir ./dataset-experiment collect ...` のように
サブコマンドの前に置く。

## やっていること（収集パイプライン）

`bp collect --project <project>.yaml` を実行すると、対象リポジトリの各Issueについて
次の処理を行う（`bug_prospector/core/collector.py` の `Collector._build_record`）。

1. **Issue検索**: `search.labels` 等の条件でGitHub Issue検索APIを叩く（`state:closed`
   かつ `linked:pr` で、マージ済みPRが紐付くものだけを対象にする）。
2. **PR特定**: IssueのタイムラインからリンクされたマージPRを取得し、その
   `merge_commit` を **fixコミット**、その親コミットを **buggyコミット** とする。
3. **diff取得**: buggy→fix の unified diff、変更ファイル一覧、追加/削除行数を取得。
4. **テスト検証**（`validation.enabled: true` のとき）:
   `reverse_patch_source` 戦略 — fixコミットのワークツリーでソースファイルだけを
   逆パッチ適用し（**テストファイルはfix版のまま残す**）、
   - fix状態でテスト実行 → 基本PASSのはず
   - 逆パッチ後（＝バグ状態）でテスト実行 → 差分が有効なら FAIL するはず
   - buggyでFAILしていたテストがfixでPASSに変わっていれば `valid`（＝そのテストが
     このバグを実際に検出する回帰テストとして機能している）、そうでなければ
     `invalid`（テストが差分に反応しない＝バグの再現・検証ができていない）。
5. 上記すべてを `BugRecord` としてSQLite（`<dataset-dir>/dataset.db`）に保存する。
   同じIssueは再実行時にスキップされる（べき等）。

**注意**: 4は実際にビルド・テストスイートを実行するため、収集は低速かつ環境依存
（ツールチェインの有無、JDKバージョン、ネットワーク等）で失敗し得る。
`validation.enabled: false` にすると、テスト実行なしで Issue・PR・diff だけを
高速に収集できる（検証は `skipped` になり、後から `bp generate-tests` の対象にもなる）。

修正にテストが同梱されていない（4で `invalid` 等になった）バグは、`bp generate-tests` で
LLMに再現テストを書かせ、同じ逆パッチ検証で FAIL→PASS を確認できたものを `llm_valid` に昇格させる
（後述）。

## プロジェクト設定ファイル（YAML）

`bp collect --project <file>.yaml` で読み込む。フィールドは `bug_prospector/config.py`
の `ProjectConfig` に対応する。リポジトリ直下の `clap.yaml` 等が実例。

```yaml
project_id: "clap-rs-clap"        # データ保存パスの一部として使われる識別子

repo:
  url: https://github.com/clap-rs/clap
  vcs_type: git

tracker:
  type: github
  url: https://github.com/clap-rs/clap
  auth:
    token: ${GITHUB_TOKEN}         # ${VAR} で環境変数を参照できる

search:
  labels: [C-bug]                 # プロジェクトごとにバグラベルの表記が違うので要確認
  state: closed
  limit: 500                      # GitHub検索で取得する最大件数
  since: "2011-01-01"
  require_pr: true                # true: マージ済みPRが紐付かないIssueはスキップ

language:
  type: rust                      # rust | java | python（言語アダプタ名）
  test_patterns:                  # テストファイル判定パターン（リバースパッチのソース/テスト分割用）
    - "tests/**"
  setup_commands:                 # テスト実行前に一度だけ実行する環境構築コマンド
    - "cargo build --quiet"
  test_command: "cargo test"      # テスト実行コマンド
  test_timeout: 120               # テスト実行タイムアウト（秒）

validation:
  enabled: true                   # false にするとテスト実行なし（diff解析のみ）
  strategy: reverse_patch_source  # reverse_patch_source | reverse_patch_all
  save_test_output: true          # true で dataset/test-logs/{project_id}/ にログ保存
  show_failed_tests: true         # true で失敗テスト名をログ出力

llm:
  enabled: false                  # true で bp generate-tests が使用可能になる
  provider: claude_code           # 現状はclaude_codeのみ（claude -p をサブプロセス起動）
  max_attempts: 3                 # 1レコードあたりの生成・検証リトライ上限
  max_context_chars: 20000        # プロンプトに埋め込むIssue本文/diffの文字数上限
  claude_code:
    command: "claude"             # PATH上のclaudeコマンド名、またはフルパス
    allowed_tools: "Read,Grep,Glob,Write,Edit"
    timeout: 600
    rust_bottom_up: true          # Rust向けのボトムアップ構築ガイダンスをプロンプトに付けるか
```

`--project` を使わない簡易実行もできる（`bp collect --tracker-url <url> --token <token>`）。
この場合 `validation`/`llm` は無効のまま、最小限の設定で収集する。

## CLIコマンド

### `bp collect`

```
bp collect --project clap.yaml [--limit 10] [--since 2020-01-01] [--labels bug,regression] [--dry-run]
```

- `--project/-p`: プロジェクトYAML（推奨）。CLIオプションはYAMLの値を上書きする。
- `--dry-run`: GitHub検索・PR特定までは実行するが、クローン/ビルド/テスト/保存は一切行わない。
  ラベル名や検索条件が正しいかを安全に確認できる。

### `bp stats`

```
bp stats [--tracker <URLの前方一致>] [--json]
```

収集済みデータセット全体の統計を表示する: 言語・トラッカー別件数、fixのサイズ
（追加/削除行数、変更ファイル数）、Issueのクローズまでの日数、検証ステータスの内訳、
テストケース単位のFAIL→PASS集計。

### `bp delete`

```
bp delete --tracker <URLの前方一致> [--status <validationステータス>] [--bug-id <ID>]... [--dry-run] [--yes]
```

収集済みレコードを削除し、再収集できる状態に戻す。バグ修正後に古いデータを
やり直したい場合等に使う。事故防止のため `--tracker`/`--status`/`--bug-id` の
いずれか最低1つの絞り込みが必須（無指定でデータセット全体を消すことはできない）。
削除時は対象レコードの一覧を表示し、`--yes`が無ければ確認を求める。
`dataset/patches/`・`dataset/test-logs/`配下の対応するパッチ・ログファイルも
あわせて削除する。

```bash
# 何が消えるかだけ確認
bp delete --tracker https://github.com/BurntSushi/ripgrep --status error --dry-run

# 実際に削除して bp collect で再収集できる状態に戻す
bp delete --tracker https://github.com/BurntSushi/ripgrep --status error --yes
```

### `bp generate-tests`

テストが同梱されていない（fixのdiffにテストファイルの変更がない）ため `invalid` 等のまま
バグとして確認できていないレコードに対し、LLM（既定では `claude -p`、[Claude Code
CLI](https://docs.claude.com/claude-code) の非対話モード）で再現テストを合成し、
fixコミットでPASS・buggyコミット（ソース差分のみ逆パッチ）でFAILすることを機械的に確認した
うえで `validation.status: llm_valid` に昇格させる。設計・実装の詳細は
[doc/test-generation.md](doc/test-generation.md) を参照。

```
bp generate-tests --project <project>.yaml [--tracker <URLの前方一致>] [--status <validationステータス>]
                   [--bug-id <ID>]... [--limit N] [--max-attempts N] [--dry-run]
```

- プロジェクトYAMLに `llm.enabled: true` が必要（既定は無効）。
- 対象は「buggy/fixコミットが両方解決済み」「fixのdiffにテスト変更を含まない」「validationが
  まだFAIL→PASSを確認できていない状態（`invalid`/`setup_failed`/`patch_failed`/`timeout`/`error`/
  `skipped`）」「未確認の生成テストで既にconfirmed済みでない」をすべて満たすレコード。
- `--dry-run` で候補一覧のみ表示し、LLM呼び出しは行わない。
- Rust（`language.type: rust`）のレコードでは、[RUG論文](doc/rug-paper-summary.md)の
  「型依存をノード単位に分解し、コンパイラ検証しながらボトムアップに積み上げる」という
  アイデアをプロンプトに取り込み、`cargo check` による自己検証を促す（詳細は
  [doc/test-generation.md](doc/test-generation.md) 6.1.2節、検討の経緯は
  [doc/rug-integration-note.md](doc/rug-integration-note.md)）。Python/Javaは対象外で挙動は変わらない。
- 失敗（未確認のまま試行上限到達）したレコードの状態は永続化されないため、専用フラグなしに
  次回実行で自動的に再挑戦の対象になる。
- 生成テストのソースは `BugRecord.reproduction.generated_test.source_code` として
  `dataset.db` に保存される。ファイルとして確認したい場合は `bp export-tests` で書き出す。
- `claude -p` 1回ごとのトークン使用量・コスト・ターン数は
  `<dataset-dir>/llm-usage/<project_id>.jsonl` に追記され、実行終了時にレコード単位の集計表が表示される。

```bash
# 候補件数と対象レコードのみ確認（LLM呼び出しなし）
bp generate-tests --project clap.yaml --dry-run

# 実際に生成・検証する（クローン済みリポジトリ・cargo等のツールチェインが必要）
bp generate-tests --project clap.yaml --limit 5
```

### `bp export-tests`

```
bp export-tests [--tracker <URLの前方一致>] [--bug-id <ID>]... [--out-dir <dir>]
```

`bp generate-tests` が `dataset.db` に保存した再現テストをファイルとして書き出す。
出力先（デフォルト `<dataset-dir>/generated-tests`）の下に、レコードごとに
`<bug_id>/<生成時のテストファイルパス>` と、Issue URL・buggy/fixコミット・モデル・試行回数等を
記録した `<bug_id>/meta.json` を作る。既存ファイルは上書きする。

```bash
bp export-tests --tracker https://github.com/clap-rs/clap
```

## 出力されるメトリクスの意味

### Validation status（検証ステータス）

| ステータス | 意味 |
|---|---|
| `valid` | buggy状態でFAILし、fix状態でPASSしたテストが1件以上ある＝バグを検出する回帰テストが機能している |
| `llm_valid` | 元のfixにはテストが無かったが、`bp generate-tests` がLLMで合成した再現テストでFAIL→PASSを確認できた（`valid`とは出自が異なるため区別している。[doc/test-generation.md](doc/test-generation.md)参照） |
| `invalid` | 逆パッチしても差分に反応するテストが無かった（両方PASS/両方FAIL）＝そのテストではこのバグを再現できない |
| `patch_failed` | 逆パッチの適用に失敗した |
| `setup_failed` | 環境構築コマンド（`setup_commands`）が失敗した |
| `error` | 検証処理自体で例外が発生した |
| `skipped` | `validation.enabled: false` のため検証を行わなかった |

## 対応言語アダプタと制約

| 言語 | テスト出力パーサ | 既知の制約 |
|---|---|---|
| Rust | `cargo test` の verbose/quiet両対応（`adapters/language/rust.py`） | ワークスペース構成では、生成テストの置き場所（ルートの `tests/` か各クレートの `tests/` か）によって `cargo test` の対象が変わる |
| Java | Maven Surefire / Gradle test（`adapters/language/java.py`） | マルチモジュールプロジェクトは `language.test_command` を対象モジュールに明示的に絞り込む必要がある（`gson.yaml`/`log4j2.yaml` 参照）。Surefire / Gradle の標準出力からは失敗したテスト名しか取れないため、ケース単位の判定は全体の終了コードへのフォールバックに頼る場面が多い |
| Python | pytest（verbose/quiet両対応、`adapters/language/python.py`） | ラベル表記（`bug` / `Bug` 等）やテスト依存関係のインストール方法（extras名 or requirements ファイル）がプロジェクトごとに違うため、`setup_commands` を個別に確認する必要がある |

## データの保存場所

`<dataset-dir>` 配下（デフォルト `./dataset`）:

```
dataset.db                            # BugRecord本体（SQLite）
repos/<repo-slug>/                    # リポジトリのローカルクローン
worktrees/<repo-slug>/                # 検証用の永続ワークツリー（validation.enabled時）
patches/<project_id>/<bug_id>.patch   # 適用した逆パッチ（save_test_output等と同様に検証時のみ）
test-logs/<project_id>/<bug_id>-{fix,buggy}.log   # テスト出力（save_test_output: true時）
llm-usage/<project_id>.jsonl          # bp generate-tests の claude -p 呼び出しごとのトークン使用量・コスト
generated-tests/<bug_id>/             # bp export-tests の出力（テストファイル + meta.json）
```

`bp generate-tests` が合成した再現テストのソースの正本は `dataset.db` 内の
`BugRecord.reproduction.generated_test.source_code` で、`generated-tests/` は
`bp export-tests` で書き出したコピー。

## 設定済みプロジェクト

リポジトリ直下に、次の9プロジェクトのYAML設定を用意している。

| 言語 | プロジェクト | YAML |
|---|---|---|
| Rust | clap-rs/clap | `clap.yaml` |
| Rust | serde-rs/serde | `serde.yaml` |
| Rust | BurntSushi/ripgrep | `ripgrep.yaml` |
| Java | google/gson | `gson.yaml` |
| Java | jhy/jsoup | `jsoup.yaml` |
| Java | apache/logging-log4j2 | `log4j2.yaml` |
| Python | pallets/flask | `flask.yaml` |
| Python | fastapi/fastapi | `fastapi.yaml` |
| Python | psf/requests | `requests.yaml` |

## 開発

```bash
uv run --extra dev pytest -q
```

より詳しい設計・データモデルは `doc/` を参照（`doc/README.md` が索引）。ただし
`doc/architecture.md`・`doc/cli-spec.md`・`doc/requirements.md` は設計初期の
ドキュメントで、実際のパッケージ構成やCLIコマンドとは差異がある
（`doc/cli-spec.md` 末尾に差分の注記あり）。実装の実態は本READMEおよび
`bug_prospector/` 配下のコードを正とする。
