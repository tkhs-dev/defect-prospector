# Rust / Java / Python テスト厳密さ比較 — 実験設計

このドキュメントは `bp compare` を使って行う「バグ修正時にどれだけテストを
重視して書いているか」の言語間比較実験の設計をまとめたもの。CLI・設定ファイル
自体の使い方は [../README.md](../README.md) を参照。

## 1. 目的

同じ「バグ修正PR」というイベントを、Rust / Java / Python の複数プロジェクトで
横並びに観測し、次を定量的に比較する。

- 修正PRがテストファイルを変更している割合（`pr_test_change_rate`）
- 修正1件あたりで変更された関数数・新規に追加されたテスト数の比率
  （`tests_added_per_function`） — 変更量に対してどれだけ新規テストで
  裏付けているか
- バグ修正前の既存テストが、修正差分の触れた行をどれだけカバーしていたか
  （`diff_coverage_rate`） — 0%に近いほど「未テストのコードでバグが起きた」
- プロジェクト全体の行/分岐カバレッジ率

前者2つは `bug_prospector/core/test_effort.py` によるdiffのテキスト解析のみで
完結し、テスト実行を必要としない（高速・失敗しにくい）。後者2つは実際に
ビルド・テストスイート・カバレッジツールを実行する必要があり、低速かつ
環境依存で失敗し得る。

## 2. 対象プロジェクトの選定

各言語3プロジェクトずつ、計9プロジェクトを選定した（`clap.yaml` 等、
リポジトリ直下のYAMLファイル）。選定基準:

- GitHub Issuesで明確な「バグ」ラベルが運用されていること（`bp collect` は
  `search.labels` でラベル検索するため、ラベルが存在しない/曖昧なプロジェクトは
  比較対象にできない）
- ビルド・テストが `bp collect` のツールチェイン前提（Rust: cargo、
  Java: Maven/Gradle、Python: pytest）で自動実行できること
- カバレッジ計測が現実的に成立すること（後述）

### 採用したプロジェクト

| 言語 | プロジェクト | バグラベル | 備考 |
|---|---|---|---|
| Rust | clap-rs/clap | `C-bug` | ワークスペース構成（本体は `clap_builder`） |
| Rust | serde-rs/serde | `bug` | |
| Rust | BurntSushi/ripgrep | `bug` | ワークスペース構成、CLI統合テストが多い |
| Java | google/gson | `bug` | マルチモジュールMaven。`gson/` サブモジュールに限定して収集 |
| Java | jhy/jsoup | `bug` | 単一モジュールMaven。最もシンプルな構成 |
| Java | apache/logging-log4j2 | `bug` | マルチモジュールMaven。`log4j-core`(+`log4j-api`)に限定 |
| Python | pallets/flask | `bug` | |
| Python | fastapi/fastapi | `bug` | 旧 `tiangolo/fastapi`。組織移管済みのためURL要注意 |
| Python | psf/requests | `Bug`（大文字始まり） | `requirements-dev.txt` にテスト依存一式あり |

### 除外した候補とその理由

比較を成立させる上で技術的に無理があった、または比較の公平性を損なうと
判断した候補:

- **FasterXML/jackson-\*、apache/commons-lang** — GitHub Issueに「バグ」を
  示すラベルが存在しない（JIRA中心の運用、またはバージョン/コンポーネント
  ラベルのみ）。
- **square/retrofit、junit-team/junit5** — GradleプロジェクトでJaCoCoが
  未適用（`retrofit`）、または `jacocoTestReport` とは異なるタスク名の
  複雑な集約構成（`junit5` の `jacocoAggregation`）になっており、
  pomを変更しないad hoc実行が成立しない。
- **square/okhttp** — 実装の多くがKotlinで、Java比較の対象として不適切。
- **h2database/h2database** — 標準的なMaven/Gradleを使わない独自ビルドで、
  `bp collect` の言語アダプタが前提とするビルドコマンドが通用しない。
- **scrapy/scrapy** — テスト専用のextrasグループが無く、Twisted依存の
  非同期テストが複雑で、収集の安定性（成功率）が他候補より低いと判断。

## 3. カバレッジ計測の実装（言語ごとの違い）

| 言語 | ツール | 実行方法 |
|---|---|---|
| Rust | cargo-llvm-cov | `cargo llvm-cov --workspace --cobertura --output-path target/llvm-cov-cobertura.xml`。`--workspace` は必須（後述） |
| Java | JaCoCo | pomを変更せず、フルGAV座標でad hoc実行: `mvn org.jacoco:jacoco-maven-plugin:0.8.12:prepare-agent test org.jacoco:jacoco-maven-plugin:0.8.12:report` |
| Python | coverage.py（pytest-cov） | `pytest --cov=<package> --cov-report=xml:coverage.xml -q`。出力はCobertura互換のため、Rustと同じXMLパーサ（`adapters/coverage/cobertura.py`）を再利用している |

レポートのdiff coverage計算（`bug_prospector/core/coverage.py`）は、修正diffの
「削除・変更された旧ファイル側の行番号」を、カバレッジレポート上の
instrumented行と突き合わせることで行う。JaCoCoはファイルパスがソースセット
相対（例: `com/example/Foo.java`）でリポジトリルート相対と一致しないことが
あるため、末尾一致でも照合するようにしている。

## 4. clapでの動作実験で判明した注意点

本番規模での収集の前に、`clap.yaml` を使って少数のIssueで実際にパイプラインを
動かし、以下を確認・修正した（`dataset-experiment/` に隔離して実施）。

- **`cargo-llvm-cov` は `--workspace` を付けないとルートパッケージしか計装しない。**
  clapはワークスペースのルートパッケージ自体は薄いラッパーで、実装本体は
  `clap_builder` にあるため、`--workspace` 無しでは全体行カバレッジが実質0%に
  なる（73行のみ計装）。付けると14,000行超・約82%という妥当な値になることを
  手動再現で確認済み。
- **一部のIssueでカバレッジ計測だけが失敗することがある。** 原因を手動再現した
  ところ、`cargo-llvm-cov` が付与する `-C instrument-coverage --cfg=coverage`
  付きビルドで、特定のderiveマクロ経由テスト（`tests/derive/*.rs`）のコンパイルが
  決定論的に失敗する（同一コマンドを再実行しても同じ箇所で失敗し、
  プロダクトコード側の問題ではなく `cargo-llvm-cov`・rustc・proc-macroの
  組み合わせに起因すると考えられる）。この場合 `coverage.status="error"` として
  記録されるだけで、`validation`（FAIL→PASS判定）自体は正常に行われる。
- **永続ワークツリーはIssueごとに `target/` を含め `git clean -fd` される。**
  そのためカバレッジ計測は毎回ワークスペース全体をゼロからビルドすることになり、
  1Issueあたり数十秒〜数分かかる。大規模収集を行う場合はこの点を踏まえて
  `coverage.timeout`（現在 `clap.yaml`/`serde.yaml`/`ripgrep.yaml` は1200秒）を
  見積もる必要がある。

## 5. 実行方法

```bash
# 1プロジェクトずつ収集（--dataset-dir を分ければ本番データと混ざらない）
bp collect --project clap.yaml
bp collect --project serde.yaml
bp collect --project ripgrep.yaml
bp collect --project gson.yaml
bp collect --project jsoup.yaml
bp collect --project log4j2.yaml
bp collect --project flask.yaml
bp collect --project fastapi.yaml
bp collect --project requests.yaml

# 言語間比較
bp compare --languages rust,java,python
```

新しいプロジェクトを対象に加える場合は、上記「2. 対象プロジェクトの選定」の
基準（バグラベルの有無、ビルド前提、カバレッジの成立可否）を先に
`bp collect --dry-run` やカバレッジコマンドの手動実行で確認してから
YAMLを追加すること。
