# defect-prospector

GitHub上のバグ修正PRを収集し、リバースパッチによるFAIL→PASS検証・カバレッジ計測・
「テストをどれだけ重視して書いているか」を示すメトリクスを算出するツール。
Rust / Java / Python のプロジェクトを横断して、バグ修正時のテストの書き方の
厳密さを定量的に比較調査することを目的としている。

設計時点のドキュメントは `doc/` にあるが、実装とはズレている部分が多い
（`doc/cli-spec.md` の「11. 実装済みの拡張コマンド」参照）。**本READMEが
現在の実装の実態に即した一次情報**。詳しい研究デザインは
[doc/test-rigor-comparison.md](doc/test-rigor-comparison.md) を参照。

## セットアップ

```bash
uv sync --extra dev
```

`.env` に GitHub Personal Access Token を置くと自動で読み込まれる（`bug_prospector/cli/main.py` が起動時に `load_dotenv()` を呼ぶ）。

```
GITHUB_TOKEN=ghp_xxxxxxxxxxxx
```

言語ごとに、実際にテスト検証・カバレッジ計測を行うためのツールチェインが別途必要:

| 言語 | 必要なツール |
|---|---|
| Rust | `cargo`、カバレッジ計測を使うなら `cargo install cargo-llvm-cov` |
| Java | JDK、Maven または Gradle（プロジェクトにより異なる。`gradlew` があればラッパー経由で実行） |
| Python | `pip`（各プロジェクトの `setup_commands` で `pytest`/`pytest-cov` 等をインストール） |

## クイックスタート

```bash
# 1件も保存せずに、検索条件やラベルが正しいか確認する
bp collect --project clap.yaml --limit 3 --dry-run

# 実際に収集する（GitHub検索 → PR特定 → diff取得 → テスト検証 → カバレッジ計測 → 保存）
bp collect --project clap.yaml --limit 20

# 収集済みデータセットの統計を見る
bp stats

# 複数言語のテスト重視度・カバレッジを比較する
bp compare --languages rust,java,python
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
5. **カバレッジ計測**（`coverage.enabled: true` のとき、バグ状態のワークツリーで実行）:
   言語ごとのカバレッジツールを実行し、
   - プロジェクト全体の行/分岐カバレッジ率
   - **diff coverage**: 修正差分が触れた行（＝バグ状態でも存在した行）のうち、
     バグ修正前の既存テストで計装・カバーされていた行の割合
   を記録する。0%に近いほど「そもそも誰もテストしていなかったコードでバグが起きた」
   ことを意味する。
6. 上記すべてを `BugRecord` としてSQLite（`<dataset-dir>/dataset.db`）に保存する。
   同じIssueは再実行時にスキップされる（べき等）。

**注意**: 4と5はどちらも実際にビルド・テストスイートを実行するため、収集は
低速かつ環境依存（ツールチェインの有無、JDKバージョン、ネットワーク等）で失敗し得る。
`validation.enabled: false` にすると、diffだけを解析する軽量な指標（後述の
「テスト重視度」）のみで、テスト実行なしに高速・安全に収集できる。

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

coverage:
  enabled: true                   # validation.enabled=true のときのみ意味を持つ
  tool: cargo-llvm-cov             # 参考情報。空なら言語アダプタのデフォルトツールを使う
  command: ""                     # 空なら言語アダプタのデフォルトコマンドを使う
  report_path: ""                 # 空なら言語アダプタのデフォルトパスを使う
                                   # （マルチモジュールJava等でデフォルトが合わない場合に上書き）
  timeout: 1200
```

`--project` を使わない簡易実行もできる（`bp collect --tracker-url <url> --token <token>`）。
この場合 `validation`/`coverage` は無効のまま、最小限の設定で収集する。

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
（追加/削除行数、変更ファイル数）、Issueのクローズまでの日数、テストケース単位の
FAIL→PASS集計、そして下記の「テスト重視度」セクション。

### `bp compare`

```
bp compare --languages rust,java,python [--json]
```

`reproduction.environment.language` で言語ごとにレコードをグルーピングし、PRの
テスト変更率・テスト重視度・カバレッジを言語間で比較表示する。本ツールの主目的
（言語間のテスト厳密さ比較）に対応するコマンド。

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

## 出力されるメトリクスの意味

### Validation status（検証ステータス）

| ステータス | 意味 |
|---|---|
| `valid` | buggy状態でFAILし、fix状態でPASSしたテストが1件以上ある＝バグを検出する回帰テストが機能している |
| `invalid` | 逆パッチしても差分に反応するテストが無かった（両方PASS/両方FAIL）＝そのテストではこのバグを再現できない |
| `patch_failed` | 逆パッチの適用に失敗した |
| `setup_failed` | 環境構築コマンド（`setup_commands`）が失敗した |
| `error` | 検証処理自体で例外が発生した |
| `skipped` | `validation.enabled: false` のため検証を行わなかった |

### Test discipline（テスト重視度、`bug_prospector/core/test_effort.py`）

diffのテキストを言語別の正規表現ヒューリスティックで解析し、**テスト実行なし**で
算出する指標。AST等の厳密な構文解析はしていないベストエフォートの近似値であり、
特に `functions_changed` は「シグネチャ行が diff の前後コンテキストに含まれない
変更（大きな関数の奥深くだけの変更）」を数えられないため下限寄りに振れる。

| 指標 | 意味 |
|---|---|
| `functions_changed` | ソースコード側で差分が触れた関数/メソッド数の近似値 |
| `tests_added` | テストファイル側で新規に追加されたテスト宣言数（既存テストの修正は含まない） |
| `test_files_changed` / `test_lines_added` | 変更されたテストファイル数・追加行数 |
| `tests_added_per_function` | `tests_added / functions_changed`。関数の変更量に対してどれだけ新規テストで裏付けているかを示す中心指標 |
| `pr_test_change_rate`（`bp compare`） | 修正PRのうちテストファイルに変更があった割合 |

### Coverage（カバレッジ、`validation.enabled` かつ `coverage.enabled` が両方true）

| 指標 | 意味 |
|---|---|
| `overall_line_rate` / `overall_branch_rate` | バグ状態のワークツリーでのプロジェクト全体の行/分岐カバレッジ率 |
| `diff_coverage_rate` | 修正差分が触れた行（バグ状態にも存在する行）のうち、バグ修正前の既存テストでカバーされていた行の割合。0%に近いほど「未テストのコードでバグが起きた」ことを示す |
| `status: error` | カバレッジツールの実行やレポート解析に失敗（後述の既知の制約を参照）。この場合でも `validation` 自体の判定には影響しない |

## 対応言語アダプタと制約

| 言語 | テスト出力パーサ | カバレッジツール | 既知の制約 |
|---|---|---|---|
| Rust | `cargo test` の verbose/quiet両対応（`adapters/language/rust.py`） | cargo-llvm-cov（Cobertura出力） | ワークスペース構成では `--workspace` が必須（付けないとルートパッケージしか計装されない）。一部のproc-macro系テストで instrument-coverage との非互換が起きることがある（該当Issueは `coverage.status=error` として記録され集計から除外されるのみで、収集全体は止まらない） |
| Java | Maven Surefire / Gradle test（`adapters/language/java.py`） | JaCoCo（pomへの追加設定なしでフルGAV指定のad hoc実行） | マルチモジュールプロジェクトは `language.test_command`/`coverage.command`/`coverage.report_path` を対象モジュールに明示的に絞り込む必要がある（`gson.yaml`/`log4j2.yaml` 参照）。GradleプロジェクトはJaCoCoプラグインが未適用だと動かないため、事前に確認が必要 |
| Python | pytest（verbose/quiet両対応、`adapters/language/python.py`） | coverage.py（pytest-cov、Cobertura互換のXML出力を流用） | ラベル表記（`bug` / `Bug` 等）やテスト依存関係のインストール方法（extras名 or requirements ファイル）がプロジェクトごとに違うため、`setup_commands` を個別に確認する必要がある |

## データの保存場所

`<dataset-dir>` 配下（デフォルト `./dataset`）:

```
dataset.db                            # BugRecord本体（SQLite）
repos/<repo-slug>/                    # リポジトリのローカルクローン
worktrees/<repo-slug>/                # 検証用の永続ワークツリー（validation.enabled時）
patches/<project_id>/<bug_id>.patch   # 適用した逆パッチ（save_test_output等と同様に検証時のみ）
test-logs/<project_id>/<bug_id>-{fix,buggy}.log   # テスト出力（save_test_output: true時）
```

## 実験対象プロジェクト

`bp compare` での言語間比較のために用意した9プロジェクトのYAML設定
（選定理由・除外した候補は [doc/test-rigor-comparison.md](doc/test-rigor-comparison.md) 参照）。

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
