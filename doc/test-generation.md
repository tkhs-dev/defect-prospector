# LLMによる再現テスト生成 設計書 — defect-prospector

## 1. 背景・課題

`bp collect` の検証フロー（`Validator.validate`, `reverse_patch_source` 戦略）は、修正コミットの diff を
`split_diff()` でソース差分とテスト差分に分割し、ソース側のみを fix コミットのスナップショットへ逆パッチ適用して
テストを実行する。これにより、

- fix の diff に **新規/修正されたテストが含まれる場合**は、そのテスト単体が
  `buggy: FAIL → fix: PASS` に転じたかを `_is_valid()` がケース単位で判定でき、
  バグ混入とは無関係な既存の失敗（flaky・環境依存）を正しく除外できる。

しかし、fix の diff に **テストファイルの変更が一切含まれない場合**（`split_diff()` の `test_diff` が空）、
そのバグを狙い撃ちするテストがそもそも存在しない。この場合 `_is_valid()` は
「テストスイート全体の結果が FAIL→PASS に転じたか」という弱いフォールバックに頼るしかないが、
元々どのテストもその回帰を踏んでいない以上これはまず起こらない。結果として、

- 実際には実在し修正もされている（Issue + マージ済みPRが存在する）バグが
  `validation.status = "invalid"` として記録され、「バグとして確認できなかったもの」に分類されてしまう
- そのレコードには**バグを再現する手段（落ちるテスト）が一切残らない**

`bp stats` の `[Test cases] fallback (overall)` / `[Validation status] invalid` の件数が、
このテスト非同梱ケースのおおよその規模を示す。

## 2. ゴール / 非ゴール

### ゴール

テストが同梱されていない bug record を対象に、LLM を用いて

1. **buggy コミットでは FAIL**（バグを再現する）
2. **fix コミットでは PASS**（修正により解消されることを確認する）

という性質を満たす再現テストを合成し、既存の worktree・テスト実行基盤（`Validator`）で機械的に
検証したうえでデータセットに追加する。これにより、現状 `invalid` / フォールバック止まりで
再現手段を持たないバグ record に、実行可能な再現テストを与える。

### 非ゴール（本設計のスコープ外）

- 正常系（バグではない）レコードへのテスト追加
- 生成したテストを元プロジェクトへ PR として送り返す機能
- ファジングや大規模な複数ファイルにまたがる再現スクリプトの生成
- 既にテストが同梱されている（`test_diff` が空でない）レコードの再生成

## 3. 対象レコードの選定

次の条件をすべて満たす `BugRecord` を候補とする。

- `repository.buggy_commit` / `fix_commit` が両方とも空でない
  （`require_pr=false` 時の `_build_record_no_pr()` によるスケルトンレコードは対象外）
- `patch.split_diff(fix.diff, test_patterns)` の `test_diff` が空（＝修正にテストが伴っていない）
- `validation.status` が `invalid`（または `skipped` など、FAIL→PASS が確認できていない状態）
- まだ `reproduction.generated_test` が `confirmed=True` の状態で存在しない（冪等性。`Storage.exists()` による
  `bp collect` の二重収集スキップと同じ考え方）

`bp collect` の中では実行せず、別コマンド `bp generate-tests`（4節）として独立させる。LLM呼び出し＋
fix/buggy 両状態でのビルド・テスト実行を伴うため収集時より大幅にコストが高く、明示的なオプトインが妥当。

## 4. 全体フロー

```
bp generate-tests
  for each candidate BugRecord:
    1. コンテキスト収集
         - IssueInfo.title / body
         - FixInfo.diff のソース差分部分（split_diff の source_diff）
         - 変更されたファイルの buggy 時点の内容
             （Validator が作る worktree をそのまま使って読み取る。新規VCS APIは不要）
         - 既存テストファイルのサンプル1〜2本（test_patterns でマッチしたもの）
             → プロジェクトのテスト流儀（フレームワーク・フィクスチャ・命名）を模倣させる
         - LanguageConfig.test_command / 言語種別
    2. LLM にテスト生成を依頼
         → 出力は「新規テストファイル、または既存テストファイルへの追記」を表す unified diff
    3. 検証ループ（最大 max_attempts 回）
         a. fix コミットの worktree に生成テストを適用 → テスト実行
            → 生成したテストケースが PASS であること
         b. 同じ worktree にソース差分のみ逆パッチ適用（既存の reverse_patch_source と同じ）→ テスト実行
            → 生成したテストケースが FAIL であること
         c. a/b 両方を満たせば confirmed。
            満たさなければ、実行結果（stdout/stderr の該当部分）を次の試行のプロンプトに
            フィードバックとして渡して再生成
    4. confirmed:
         - 生成テストのソースを dataset/generated-tests/<bug-id>/ に保存
         - BugRecord.reproduction.generated_test を設定
         - validation.status を "llm_valid" に更新して再保存
       未confirmed（試行上限到達）:
         - 変更を加えず、試行回数と失敗理由だけ記録（再実行時のスキップ判定に使用）
```

worktree の作成・リセット・クリーンアップ、ソース差分のみの逆パッチ適用、テスト実行、
`LanguageAdapter.parse_test_output()` によるケース単位の結果抽出はすべて `Validator` /
`core/patch.py` の既存実装をそのまま再利用する。新規に実装するのは
「LLMにテスト草案を書かせる部分」と「生成テストというもう1枚のパッチを適用する手順」だけでよい。

## 5. データモデル拡張（`core/models/record.py`）

```python
@dataclass
class GeneratedTestInfo:
    file_path: str            # テストファイルパス（リポジトリルートからの相対パス）
    function_name: str | None # 生成されたテスト関数/メソッド名
    source_code: str          # 生成されたテストの全文（または追記ブロック）
    framework: str            # "pytest" / "cargo-test" / "junit" ...
    model: str                # 使用したLLMモデル名
    attempts: int             # confirmed に至るまでに要した試行回数
    confirmed: bool           # buggy:FAIL / fix:PASS を機械的に確認できたか
    generated_at: datetime

    @classmethod
    def from_dict(cls, d: dict) -> GeneratedTestInfo: ...
```

- `ReproInfo` に `generated_test: GeneratedTestInfo | None = None` を追加する。
- `ValidationResult.status` に新定数 `LLM_VALID = "llm_valid"` を追加する。
  既存の `VALID`（プロジェクト自身のテストで確認）とは区別し、`bp stats` / `bp delete --status` /
  データセット利用側が「ネイティブな再現テストか、合成されたものか」を区別できるようにする。
  既存の `_is_valid()` ロジックは変更しない（あくまで今回のフローは並行して走る別経路）。

## 6. コンポーネント設計

### 6.1 `adapters/llm/` — LLMAdapter

既存の `adapters/{vcs,tracker,language}` と同じ ABC パターンに合わせる。

```python
# bug_prospector/adapters/llm/base.py
class LLMAdapter(ABC):
    @abstractmethod
    def generate_test(self, prompt: str, feedback: str | None = None) -> str:
        """プロンプト（と前回試行のフィードバック）から、テストを表す unified diff を返す。"""
```

組み込み実装として `AnthropicAdapter`（`adapters/llm/anthropic.py`）を用意する。
APIキーは `.env` 経由で読み込む（`GITHUB_TOKEN` と同じ `python-dotenv` の仕組みを流用し、
例えば `ANTHROPIC_API_KEY` を参照する）。

### 6.2 コンテキスト組み立て（`core/test_generator.py` 内）

- Issueタイトル・本文、`fix.diff` のソース側差分は既に `BugRecord` に保持済みのものをそのまま使う。
- 変更ファイルの buggy 時点の内容は、`Validator` が `reverse_patch_source` のために既に用意している
  worktree（ソース差分を逆パッチ適用済み＝buggy相当の状態）から直接ファイルを読み取る。
  `GitAdapter` に新しい `get_file_at` 等を追加する必要はない。
- 既存テストのサンプルは `LanguageConfig.test_patterns` でマッチしたファイルを
  worktree 内から 1〜2 本選んでそのまま埋め込み、フレームワーク・インポート・命名規約を模倣させる。
- 大きなリポジトリ（flask/ripgrep/clap/serde 等サイズの異なるプロジェクトが混在する）を想定し、
  ファイル単位・プロンプト全体の両方に文字数上限を設け、超過分は切り詰めてその旨を明記する。

### 6.3 検証ループ（`Validator` の拡張）

`Validator` に新規メソッドを追加する。

```python
def validate_with_generated_test(
    self,
    repo_path: Path,
    fix_ref: str,
    source_diff: str,
    test_patch: str,
    bug_id: str,
) -> tuple[bool, TestRunResult, TestRunResult]:
    """
    generated test が fix:PASS / buggy:FAIL になるかを確認する。
    Returns: (confirmed, fix_result, buggy_result)
    """
```

処理内容:
1. 既存の worktree 確保ロジック（永続/一時、`git reset --hard` + `git clean -fd` によるリセット）を再利用し、
   `fix_ref` のスナップショットを用意する。
2. `test_patch`（LLM が出力した新規テストの diff）を `git apply` で適用 → テスト実行。
   生成したテストケースのみを `lang_adapter.parse_test_output()` の結果から名前で引き当て、PASS かを見る。
   ここでのビルド/適用失敗は record 全体の `setup_failed` 等にはせず、LLMへのフィードバックとして扱う。
3. 続けて `source_diff` のみを逆パッチ適用（既存の `reverse_patch_source` と同一戦略）→ テスト実行。
   生成テストが FAIL になっているかを同様に確認する。
4. `finally` で worktree を `fix_ref` に `reset --hard` + `clean -fd`
   （既存の `Validator.validate()` の後片付けと同じ）し、永続 worktree に生成テストの痕跡を残さない。

**既存テスト全体への影響は見ない**（他のテストの flaky な挙動は無視し、生成テスト単体の FAIL/PASS だけを見る）。
これが、今回の問題の根本原因である「スイート全体のフォールバック判定に頼らざるを得ない」状況を回避する核心。

### 6.4 `TestGenerator`（オーケストレーター）

`core/test_generator.py` に、`Collector` と同じ粒度で以下を行うクラスを追加する。

```
TestGenerator.run(records, llm_config, max_attempts) -> GenerationSummary
  for each candidate:
    attempt = 1
    feedback = None
    loop:
      diff = llm.generate_test(prompt, feedback)
      confirmed, fix_result, buggy_result = validator.validate_with_generated_test(...)
      if confirmed: 保存して次のレコードへ
      if attempt == max_attempts: 失敗として記録し次のレコードへ
      feedback = 前回の fix_result / buggy_result の該当テストの出力
      attempt += 1
```

## 7. 設定ファイル拡張

`example-project.yaml` に対応する新ブロック（既存の `validation:` と同じ階層）。

```yaml
llm:
  enabled: false
  provider: anthropic          # LLMAdapter の実装選択
  model: "claude-sonnet-5"
  max_attempts: 3
  max_context_chars: 20000     # プロンプトに埋め込むコンテキストの文字数上限
```

`ProjectConfig` に `LLMConfig` dataclass を追加し、`ValidationConfig` と同様 `_from_dict` でパースする。

## 8. CLI仕様: `bp generate-tests`

既存の `collect` / `stats` と同じ `click` グループ配下のサブコマンドとして追加する。

```
bp generate-tests [options]
```

| オプション | 説明 |
|---|---|
| `--project PATH` / `-p` | プロジェクトYAML（`llm:` ブロックを含む） |
| `--tracker PREFIX` | トラッカーURLの前方一致で絞り込み |
| `--status STATUS` | 対象とする現在の `validation.status`（デフォルト: `invalid`） |
| `--bug-id ID` | 対象を特定のバグIDに限定（複数指定可） |
| `--limit N` | 処理するレコード数の上限 |
| `--max-attempts N` | `llm.max_attempts` を上書き |
| `--dry-run` | 候補件数と想定LLM呼び出し回数のみ表示し、API呼び出しは行わない |
| `--retry-failed` | 前回 `llm_test_failed` だったレコードも対象に含める（デフォルトはスキップ） |

### 出力例

```
Generating reproduction tests for 42 candidate record(s)...
  github-pallets-flask-1522: confirmed (2 attempts)
  github-pallets-flask-1690: failed after 3 attempts, skipping
  ...
Generated: 31 confirmed | 11 failed | 0 skipped
```

`collect` の `Collected: N new | N skipped | N failed` という既存の要約スタイルに合わせる。

## 9. `bp stats` への反映

`[Validation status]` の内訳表示に `llm_valid` が自然に乗る（`val_counts` は `r.validation.status` を
そのまま集計しているため、ステータス定数を追加するだけで追加改修なく表示される）。
加えて、「確認済みバグのうちどれだけがプロジェクト自身のテストによるものか／LLM合成によるものか」を
一目で見られるよう、`[Test cases]` セクションに次の行を追加する。

```
[Validation status]
  valid                                   120  ( 45.0%)  #########
  llm_valid                                38  ( 14.3%)  ###
  invalid                                   ...
```

## 10. 安全性・コスト制御

- **実行環境**: 生成されたテストコードは信頼できない入力として扱う。実行は常に使い捨ての worktree
  （既存の `Validator` の一時/永続 worktree 機構）の中で行い、開発者の作業コピーには一切触れない。
  `LanguageConfig.test_timeout` を上限として機能させる（既存の仕組みをそのまま適用）。
- **将来課題**: 現状の `subprocess.run(..., shell=True)` ベースの実行はネットワーク／ファイルシステムへの
  アクセスを制限しない。利用規模が拡大する場合はコンテナ等でのサンドボックス実行を追加検討する
  （v1ではスコープ外とし、ドキュメントで明示するに留める）。
- **生成物の扱い**: 生成テストは `dataset/generated-tests/` 配下にのみ保存し、元リポジトリへの
  自動コミット・push は行わない。
- **コスト**: `bp collect` から独立したオプトインコマンドとし、`--dry-run` で事前に候補件数と
  想定コストを見積もれるようにする。`--limit` / `--status` / `--tracker` / `--bug-id` による絞り込みで
  実験規模を制御する（`bp delete` の既存フィルタ設計と一貫させる）。
- **冪等性**: 既に `confirmed=True` のレコードは再実行時に自動スキップし、失敗レコードは
  `--retry-failed` を明示しない限りスキップする。

## 11. リスク・オープン課題

- **偽陽性**: 生成テストが「本当にそのバグを捉えている」のではなく、たまたま環境差・実行順序依存・
  flaky な挙動で buggy:FAIL / fix:PASS になっているだけの可能性がある。対策として、
  fix/buggy 双方の状態で生成テストを複数回（例: 3回）実行し、結果が安定していることを
  confirmed の条件に加えることを検討する。また `llm_valid` を `valid` と統合せず区別し続けることで、
  データセット利用者が信頼度の違いを踏まえて扱えるようにする。
- **コスト**: 1レコードあたり「LLM呼び出し × 試行回数 × (fix/buggy 各1回のビルド＋テスト実行)」が発生する。
  特に Rust/Java はビルドコストが高く、`setup_commands`（`cargo build` 等）の再実行コストが支配的になりうる。
  永続 worktree の再利用（既存機構）やインクリメンタルビルドの活用で緩和する。
- **出自の透明性**: 生成テストは元プロジェクトの成果物ではなく合成物である。`GeneratedTestInfo.model` /
  `.confirmed` と `validation.status == "llm_valid"` によって、データセットのどのレコードが
  合成テストに依存しているかを常に判別可能にしておく。

## 12. 段階的実装計画

1. `GeneratedTestInfo` モデルと `llm_valid` ステータス定数を追加する（挙動はまだ変えない）。
2. `LLMAdapter` ABC と `AnthropicAdapter`、コンテキスト組み立て、プロンプト設計を実装する
   （検証ループなし。生成結果をログ出力して目視確認できる段階に留める）。
3. `Validator.validate_with_generated_test()` と再試行・フィードバックループを実装する。
4. `bp generate-tests` CLIコマンドと `bp stats` の内訳表示更新を実装する。
5. （フォローアップ）利用規模の拡大に応じたサンドボックス実行の強化を検討する。
