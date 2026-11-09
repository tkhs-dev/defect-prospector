# Rust vs Java: テストの厳格さ比較 — 実験設計・実行手順

## 1. 背景・仮説

Rustプロジェクトからバグデータを収集する中で、「Rustのバグ修正PRはJavaに比べてテストが手薄なのではないか」という仮説が生まれた。
本ドキュメントは、この仮説を定量的に検証するための実験設計・実行手順をまとめる。

検証の考え方は単純で、**Rust向けに整備済みの収集パイプライン（`bp collect` / `Validator` / 逆パッチ検証）を、
同じ枠組みでJavaプロジェクトにも適用し、同一の指標で並べて比較する**というもの。
片方の言語だけの絶対値を見ても「雑かどうか」は判断できないため、必ず言語間の相対比較として読むこと。

## 2. 測定指標

`bp compare`（[cli-spec.md](cli-spec.md) 参照）が言語ごとに集計する指標:

| 指標 | 意味 | 「テストが雑」に近づく方向 |
|---|---|---|
| PR test-change rate | バグ修正PRのうちテストファイルを変更したものの割合 | 低いほど雑（テストを直さずにコードだけ直している） |
| test lines added / PR | PRごとのテストコード追加行数 | 低いほど雑 |
| diff coverage | 修正差分が触れた行（＝バグの原因箇所）のうち、buggyコミット時点の**既存**テストで実行されていた行の割合 | 低いほど雑（バグを生んだコードパスがそもそもテストされていなかった） |
| diff coverage = 0% の割合 | バグの原因箇所が既存テストに一切カバーされていなかったPRの割合 | 高いほど雑 |
| overall line/branch coverage | buggyコミット時点のプロジェクト全体のカバレッジ率 | 低いほど雑（参考値。プロジェクト規模やテスト対象範囲の違いに影響されやすい） |

このうち **diff coverage** が最も直接的な指標である。
「PRにテストが追加されたか」だけでは、無関係なテストの追加や、修正後にしか通らない不十分なテストも
カウントしてしまう。diff coverageは「バグの原因になった行を、修正前の時点で既存テストが実際に実行していたか」
を見るため、Codecovなどが使う "patch coverage" と同じ考え方に基づく、より的を射た指標になっている。

これらは `bp stats` が既に集計している「PRを通じたテストケース増減（FAIL→PASS等）」とも補完関係にある。
`bp stats --json` の `test_cases` セクションも合わせて見ること。

### 2.1 ビルド無し（静的解析のみ）で測定できる範囲

上表のうち **PR test-change rate** と **test lines added / PR** は `r.fix.diff`（unified diff の
テキスト）だけから算出しており（`bug_prospector/core/test_effort.py`）、ビルドもテスト実行も
必要ない。一方 **diff coverage** と **overall line/branch coverage** は「実行時にそのコード行を
テストが通ったか」という動的な性質そのものであり、原理的に静的解析だけでは代替できない
（テストスイートを計装付きで実際に走らせるしかない）。

そのため、収集フローを2段階に分けられる:

1. **静的先行測定**: `bp collect --no-validation` でビルド・テスト実行・カバレッジ計測を
   一切行わず、GitHub APIとgit diffの取得のみで収集する（4.1節）。`pr_test_change_rate` 等を
   すぐに確認でき、環境構築（cargo-llvm-cov・JDK・Maven/Gradle）なしで多数のプロジェクトを
   素早く試せる。
2. **動的計測**: 必要になった時点で `validation.enabled` / `coverage.enabled` を `true` にした
   設定で再収集し、diff coverage・overall coverageを追加で得る（4.2節以降）。既存レコードは
   スキップされるため、既に静的先行測定で収集済みのIssueに対しても再実行すればそのまま
   動的指標が追記される（実装上は同一IDなら新規保存されないため、動的計測をやり直す場合は
   一度 `bp delete` で対象レコードを削除してから再収集する必要がある点に注意）。

## 3. 対象プロジェクト

ドメインが近い Rust / Java のペアを3組選定した（2026-09時点でのGitHub上の状態に基づく）。

| ドメイン | Rust | Java | 設定ファイル |
|---|---|---|---|
| CLIパーサー | [clap-rs/clap](https://github.com/clap-rs/clap) | [remkop/picocli](https://github.com/remkop/picocli) | `clap.yaml` / `projects/picocli.yaml` |
| JSONシリアライズ | [serde-rs/json](https://github.com/serde-rs/json) | [google/gson](https://github.com/google/gson) | `projects/serde_json.yaml` / `gson.yaml` |
| 非同期HTTP/ネットワーキング | [hyperium/hyper](https://github.com/hyperium/hyper) | [eclipse-vertx/vert.x](https://github.com/eclipse-vertx/vert.x) | `projects/hyper.yaml` / `projects/vertx.yaml` |

選定基準:
- 両言語で広く使われ、アクティブにメンテナンスされている（arichived=false）
- GitHub Issues + labelでバグを識別でき、`is:closed label:<bug> linked:pr` で一定件数がヒットする
- ビルド・テストがコマンド一発（`cargo test` / `mvn test` / `./gradlew test`）で完結する

2026-09時点での `label:<bug> is:closed linked:pr` のヒット件数（参考値。実行時点で変動する）:

| プロジェクト | ラベル | 件数 |
|---|---|---|
| clap-rs/clap | `C-bug` | 304 |
| remkop/picocli | `type: bug :bug:` | 34 |
| serde-rs/json | `bug` | 10 |
| google/gson | `bug` | 46 |
| hyperium/hyper | `C-bug` | 83 |
| eclipse-vertx/vert.x | `bug` | 132 |

serde_json は母数が10件と小さいため、この組だけで結論を出さず、他2組の傾向と合わせて解釈すること。
必要なら `search.labels` に他のラベル（例: JSON関連の "parse" 等）を追加して母数を増やすことも検討する。

### 除外した候補: square/okhttp

Java側HTTPクライアントとして最初に square/okhttp（Rust側 hyper との対比）を検討したが、
`gh api repos/square/okhttp` が `lysine-dev/okhttp` にリダイレクトされることを確認した
（square/retrofit も同様に `lysine-dev/retrofit` へ）。squareのGitHub Organization自体は現存する一方、
これらのリポジトリだけが別アカウントへ転送されている経緯が本調査では確認できなかったため、
出所が不明瞭なまま比較対象に含めるのは避け、eclipse-vertx/vert.x に差し替えた。
実際に使う場合は、まず経緯を確認すること。

### 3.1 追加プロジェクト（静的指標の母数拡大用、2026-09-17追加）

3組のドメインペアだけでは各グループ3プロジェクトしかなく、特定プロジェクトの偏りに
結果が引っ張られやすい。静的先行測定（2.1節）はビルド不要で試行コストが低いため、
ドメインペアリングにはこだわらず Rust / Java それぞれ3プロジェクトずつ追加した。

| Rust | 設定ファイル | Java | 設定ファイル |
|---|---|---|---|
| [BurntSushi/ripgrep](https://github.com/BurntSushi/ripgrep) | `ripgrep.yaml` | [jhy/jsoup](https://github.com/jhy/jsoup) | `jsoup.yaml` |
| [serde-rs/serde](https://github.com/serde-rs/serde) | `serde.yaml` | [apache/logging-log4j2](https://github.com/apache/logging-log4j2) | `log4j2.yaml` |
| [tokio-rs/tokio](https://github.com/tokio-rs/tokio) | `projects/tokio.yaml` | [netty/netty](https://github.com/netty/netty) | `projects/netty.yaml` |

選定手順は3章冒頭の基準（広く使われアクティブ・archived=false・`is:closed label:<bug> linked:pr`
で一定件数ヒット）に加え、追加前に必ず `curl`（または `gh api`）でラベル別ヒット件数を確認し、
0件や取得エラーになる候補は除外した:

| プロジェクト | ラベル | 件数（2026-09-17時点） |
|---|---|---|
| BurntSushi/ripgrep | `bug` | 59 |
| serde-rs/serde | `bug` | 19 |
| tokio-rs/tokio | `C-bug` | 295 |
| jhy/jsoup | `bug` | 20 |
| apache/logging-log4j2 | `bug` | 83 |
| netty/netty | `defect`（`bug`は0件） | 81 |

- **netty/netty** はGitHub上のバグラベルが `bug` ではなく `defect` である点に注意（`projects/netty.yaml` にコメント記載済み）。
- **junit-team/junit5** も候補に挙がったが、GitHub APIへのリクエストが `301` リダイレクトを返し
  （`gh api repos/junit-team/junit5` が別のURLに転送される）、「除外した候補: square/okhttp」
  （上記）と同様に出所を確認できなかったため見送った。
- netty はcommon/buffer/transport/codec/handler等の多数サブモジュールを持つマルチモジュール
  Mavenプロジェクトで、`projects/netty.yaml` の `test_command` は絞り込みなしのルート一括
  （`mvn test -B`）をデフォルトにしている。動的計測で実際にビルドする場合は変更ファイルに
  応じて `-pl <module> -am` を検討すること（gson/vert.x/log4j2と同じ理由、下記「モノレポ・
  マルチモジュール構成への対応」参照）。

### モノレポ・マルチモジュール構成への対応

gson・vert.xはマルチモジュール構成で、実装とテストは一部のサブモジュール（`gson/`, `vertx-core/`）にしかない。
そのため `language.test_command` / `coverage.command` は `mvn -pl <module> -am ...` で対象を絞っている。
`bp collect` は差分中の変更ファイルパスをそのまま `changed_files` として記録するため、
サブモジュール外のファイルが変更されたPRも収集対象に含まれる点に注意（テスト実行自体は対象モジュールに限定される）。

## 4. 実行手順

### 4.1 ビルド無しでの静的先行測定（推奨: 最初に実行）

`validation.enabled` / `coverage.enabled` を編集する前に、まず `--no-validation` で
PR test-change rate だけを素早く確認する。cargo-llvm-cov・JDK・Mavenの導入は一切不要で、
`.env` の `GITHUB_TOKEN` さえあれば実行できる。

```bash
bp collect --project clap.yaml                --no-validation
bp collect --project projects/serde_json.yaml --no-validation
bp collect --project projects/hyper.yaml      --no-validation
bp collect --project projects/picocli.yaml    --no-validation
bp collect --project gson.yaml                --no-validation
bp collect --project projects/vertx.yaml      --no-validation
# 追加プロジェクト（3.1節）
bp collect --project ripgrep.yaml             --no-validation
bp collect --project serde.yaml               --no-validation
bp collect --project projects/tokio.yaml      --no-validation
bp collect --project jsoup.yaml               --no-validation
bp collect --project log4j2.yaml              --no-validation
bp collect --project projects/netty.yaml      --no-validation
```

上記12件は一括収集スクリプトでもまとめて実行できる:

```powershell
.\scripts\collect_all.ps1 -NoValidation     # Windows
```

```bash
./scripts/collect_all.sh --no-validation    # Linux/macOS/Git Bash
```

プロジェクト単位で確認する場合:

```bash
bp stats --tracker https://github.com/hyperium/hyper
```

言語グループで集計する場合（`pr_test_change_rate` を含む）:

```bash
bp compare --languages rust,java --json
```

### 4.2 事前準備（動的計測を行う場合）

```
# .env に GITHUB_TOKEN を設定済みであること（既存の仕組みをそのまま利用）

# Rust側: cargo-llvm-cov を一度だけ導入
cargo install cargo-llvm-cov

# Java側: JDK 17+ と Maven が使えること（Gradleプロジェクトは同梱の gradlew を使うため
# Gradle自体のインストールは不要）
java -version
mvn -version
```

### 4.3 小規模な試験実行（推奨）

いきなり全件収集せず、まずプロジェクトあたり数件で疎通確認する。

```powershell
# Windows
.\scripts\collect_all.ps1 -Limit 5
```

```bash
# Linux/macOS/Git Bash
./scripts/collect_all.sh --limit 5
```

エラーが出た場合、まず `--dry-run` でIssue検索とPR紐付けだけ確認するとよい
（テスト実行やカバレッジ計測は走らない）。

### 4.4 本収集

```powershell
.\scripts\collect_all.ps1
```

```bash
./scripts/collect_all.sh
```

1プロジェクトの収集失敗は後続プロジェクトの実行を止めない。実行後にどのプロジェクトが
失敗したかがまとめて表示される。

カバレッジ計測はテストスイートをもう一度、計装付きで実行するため、`validation.enabled` のみの
場合よりもプロジェクトあたりの所要時間が数倍になる。特に hyper / vert.x / gson はテストスイートが
大きいため、`test_timeout` / `coverage.timeout` を必要に応じて yaml 側で調整すること。

### 4.5 レポート生成

```powershell
.\scripts\report.ps1
```

```bash
./scripts/report.sh
```

`dataset/reports/` に `stats.txt` / `stats.json` / `compare.txt` / `compare.json` が出力される。
`bp compare` は `reproduction.environment.language` でグルーピングするため、Rust側・Java側それぞれ（3.1節の追加分を含め6プロジェクトずつ）の
プロジェクトが合算された数値になる。プロジェクト単位で見たい場合は
`bp stats --tracker <tracker_url>` を個別に実行する。

## 5. 実行結果 — 静的先行測定（2026-09-17実施）

4.1節の手順で12プロジェクト（Rust 6 / Java 6）を `--no-validation` で収集し、
`pr_test_change_rate`（diffがテストファイルに触れているPRの割合）を測定した結果。
diff coverage・overall coverage（動的計測が必要）はまだ未実施。

| プロジェクト | 言語 | 件数 | PR test-change rate | 新規テスト追加率 |
|---|---|---:|---:|---:|
| clap-rs/clap | Rust | 279 | 68.5% | 45.2% |
| serde-rs/json | Rust | 7 | 100.0% | 57.1% |
| hyperium/hyper | Rust | 60 | 48.3% | 36.7% |
| BurntSushi/ripgrep | Rust | 57 | 42.1% | 7.0% |
| serde-rs/serde | Rust | 15 | 73.3% | 20.0% |
| tokio-rs/tokio | Rust | 287 | 46.3% | 36.9% |
| remkop/picocli | Java | 31 | 54.8% | 22.6% |
| google/gson | Java | 45 | 77.8% | 48.9% |
| eclipse-vertx/vert.x | Java | 86 | 86.0% | 62.8% |
| jhy/jsoup | Java | 17 | 100.0% | 76.5% |
| apache/logging-log4j2 | Java | 81 | 71.6% | 49.4% |
| netty/netty | Java | 74 | 78.4% | 56.8% |

言語グループ集計（`bp compare --languages rust,java`）:

| 言語 | 総件数 | PR test-change rate |
|---|---:|---:|
| Rust | 705 | 56.0% |
| Java | 334 | 77.5% |

### 5.1 暫定的な解釈

- 当初の3ドメインペア（1章）だけで見た数値は Rust 65.6% / Java 77.8% だったが、
  プロジェクトを12件に広げると差はむしろ拡大した（Rust 56.0% / Java 77.5%）。
  「Rustのバグ修正PRはJavaに比べてテストが手薄」という仮説（1章）を否定する方向の
  結果は出ていない。
- Java側は6プロジェクト中5つが70%超で比較的一貫している。picocli（54.8%）のみやや低い。
- Rust側はばらつきが大きい: clap/serdeは68〜100%と高い一方、hyper/ripgrep/tokioは
  40%台に集中している。Rust全体の低さは主にこの3プロジェクトに引っ張られている面があり、
  「Rustは全体的にテストが手薄」と単純化するより「非同期ランタイム・CLIツール系の
  一部大規模プロジェクトでテスト変更を伴わない修正が多い」という見方の方が正確かもしれない。
- ripgrep は新規テスト追加率が7.0%と全プロジェクト中突出して低い一方、PR test-change rate
  自体は42.1%ある。既存テストの調整のみで新規テストを追加しないスタイルの可能性があり、
  「テストファイルに触れたか」と「新規テストを追加したか」は別の観点として両方見る必要がある。
- serde_json（n=7）・jsoup（n=17）・serde（n=15）は母数が小さく、1〜2件の結果に率が
  大きく動く。個別の結論には使わず、プロジェクト単位の内訳（`bp stats --tracker`）を
  必ず併記すること。
- これはあくまで **PR test-change rate という粗い静的指標** の結果であり、
  「バグの原因になった行が実際にテストで実行されていたか」を見る diff coverage
  （2章参照）とは異なる。この段階の結果だけで「テストが雑」という結論を出すのは早計で、
  動的計測（4.2節以降）で diff coverage を確認して裏付けを取ること。

## 6. 既知の制約

- **Javaのテストケース粒度**: Maven Surefire / Gradle のデフォルト出力は失敗したテスト名しか
  得られない（成功したテスト名は個別に出力されない）。そのため `bp stats` のテストケース単位の
  FAIL→PASS判定は、Rust/Python（cargo test / pytest）ほど厳密ではなく、全体の終了コードによる
  フォールバック判定に依存する場面が多い（[adapter-interface.md](adapter-interface.md) 参照）。
  より厳密にするには `target/surefire-reports/*.xml`（JUnit XML）を読む実装への拡張が必要。
- **diff coverageのパスマッチング**: cargo-llvm-cov（Cobertura形式）はリポジトリルート相対パスを
  出力するため完全一致するが、JaCoCoはソースルート相対（パッケージパスのみ）でしか記録しないため、
  末尾一致でマッチングしている。モジュール構成によっては一致しない・複数候補がヒットして
  マッチできないケースがある（その場合 diff_lines_total に含まれないだけで、誤ってカウントはしない）。
- **各yamlのコマンドは未検証の初期値**: 実際にホスト上で一通り流し、ビルド・テスト・カバレッジ
  コマンドやレポートパスが各プロジェクトの現在のバージョンと整合するか確認すること
  （特にマルチモジュール構成のgson/vert.xは変更が入りやすい）。
- **母数の偏り**: プロジェクトごとにヒット件数が大きく異なる（10件〜300件超）。単純な合算平均は
  件数の多いプロジェクトに引っ張られるため、`bp stats --tracker` でプロジェクト単位の内訳も
  必ず確認すること。
- **Windowsでのクローン失敗（長いファイルパス）**: `apache/logging-log4j2` は
  `log4j-1.2-api/src/test/resources/config-1.2/hadoop/...` のように非常に長い相対パスの
  テストリソースを含み、Windows上で `git clone` すると `Filename too long` で
  チェックアウトに失敗することがある（`fatal: cannot create directory at '...': Filename too long`）。
  ただしgitオブジェクトデータベース自体は作成されているため、`bp collect` を再実行すれば
  （`GitAdapter.ensure_repo` は `.git` の存在だけを見て再クローンせず `fetch` にフォールバックする）
  ワーキングツリーの完全なチェックアウト無しで収集を継続できる（diff抽出はgitオブジェクト経由で
  完結し、チェックアウト済みファイルには依存しないため）。恒久対応するには
  `git config --system core.longpaths true`（管理者権限が必要）を設定する。
- **3.1節の追加プロジェクトはドメインペアリングしていない**: 1章の実験設計はRust/Javaの
  ドメインペアで比較する前提だが、静的先行測定の母数拡大用に追加した6プロジェクトは
  ドメインを揃えていない（例: ripgrepに対応するJava側CLIツールは選定していない）。
  そのため5章の言語グループ集計はドメイン間の系統的な違いを均さないまま合算している点に注意。
