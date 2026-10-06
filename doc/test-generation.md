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
    1. fix コミットの worktree を用意（Validator の既存機構を再利用）
    2. 検証ループ（最大 max_attempts 回）
         a. LLMAdapter.generate_test(worktree, task, feedback) を呼ぶ
              → 実装によって手段は異なるが、結果として worktree 内に
                「新規テストファイル」または「既存テストファイルへの追記」が書き込まれる
                （詳細は 6.1 節。Claude Code のようなエージェント型は自分で直接ファイルを書き、
                チャット補完APIのような非エージェント型はdiffを生成してこのメソッド内で git apply する）
         b. そのままテスト実行 → 生成したテストケースが PASS であること（fix状態の確認）
         c. 同じ worktree にソース差分のみ逆パッチ適用（既存の reverse_patch_source と同じ）→ テスト実行
            → 生成したテストケースが FAIL であること（buggy状態の確認）
         d. b/c 両方を満たせば confirmed。
            満たさなければ worktree を fix_ref に reset --hard + clean -fd して生成物を消し、
            直前の実行結果（stdout/stderr の該当部分）をフィードバックとして次の試行へ渡す
    3. confirmed:
         - worktree 内に残っている生成テストファイルの内容・パスを読み取り
           dataset/generated-tests/<bug-id>/ に保存
         - BugRecord.reproduction.generated_test を設定
         - validation.status を "llm_valid" に更新して再保存
         - worktree を reset --hard + clean -fd（次のバグのために元に戻す）
       未confirmed（試行上限到達）:
         - 変更を加えず、試行回数と失敗理由だけ記録（再実行時のスキップ判定に使用）
```

生成したテストファイルは `test_patterns` にマッチするパス（例: `tests/test_xxx.py`）に書かせることで、
`split_diff()` ベースの逆パッチ適用（c）がこのファイルを巻き込まない（＝ソースだけが逆パッチされ、
生成テストはそのまま残る）という既存の仕組みをそのまま利用できる。worktree の作成・リセット・
クリーンアップ、ソース差分のみの逆パッチ適用、テスト実行、`LanguageAdapter.parse_test_output()`
によるケース単位の結果抽出は、すべて `Validator` / `core/patch.py` の既存実装をそのまま再利用する。
新規に実装するのは「LLMAdapterの呼び出しと試行ループ」と「生成ファイルの検出・保存」だけでよい。

## 5. データモデル拡張（`core/models/record.py`）

```python
@dataclass
class GeneratedTestInfo:
    file_path: str            # テストファイルパス（リポジトリルートからの相対パス）
    function_name: str | None # 生成されたテスト関数/メソッド名
    source_code: str          # 生成されたテストの全文（または追記ブロック）
    framework: str            # "pytest" / "cargo-test" / "junit" ...
    provider: str             # LLMAdapter の実装名（"claude_code" / "anthropic_api" / "openai_api" ...）
    model: str                # 使用したモデル名（providerの応答から取得できる範囲で記録）
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

**テスト生成には Claude Code CLI の `-p`（print / 非対話）モードを第一の実装として使う。**
一方で、特定のベンダー・特定の呼び出し方式にロックインしないよう、`adapters/{vcs,tracker,language}`
と同じ ABC パターンに合わせて差し替え可能にする。

Claude Code は Read / Write / Edit / Bash などのツールを自分で持つ**エージェント型**のCLIであり、
「プロンプト文字列を渡したら diff テキストが返ってくる」という単純なチャット補完の往復には馴染まない
（実行ディレクトリを自分で探索し、ファイルを直接書き換える方が自然で、かつコンテキスト構築の手間も省ける）。
一方、素の Anthropic Messages API や OpenAI API のような**非エージェント型**の実装はファイルシステムに
触れられないため、こちらはプロンプトに埋め込んだ情報だけから diff テキストを生成し、呼び出し側が
`git apply` で適用する必要がある。

この違いを吸収するため、`LLMAdapter` は「結果としてworktree内に変更が存在する状態になる」ことだけを
契約とする（返り値の型でdiffテキストを強制しない）。

```python
# bug_prospector/adapters/llm/base.py
@dataclass
class TestGenerationTask:
    """LLMAdapter に渡すコンテキスト。全フィールドをプロンプトに使うかはAdapter実装次第
    （エージェント型は worktree を自分で探索できるため一部を使わなくてもよい）。"""
    issue_title: str
    issue_body: str
    source_diff: str              # fix.diff のうちソース差分部分（split_diff の source_diff）
    test_patterns: list[str]      # 生成するテストファイルが満たすべきパスパターン
    language: str
    test_command: str
    sample_test_files: dict[str, str] | None = None  # 既存テストのサンプル（path -> 内容）


class LLMAdapter(ABC):
    @abstractmethod
    def generate_test(
        self,
        worktree: Path,
        task: TestGenerationTask,
        feedback: str | None = None,
    ) -> None:
        """
        worktree 内に再現テストを書き込む（新規ファイル作成、または既存テストファイルへの追記）。
        書き込み先は task.test_patterns にマッチするパスにすること
        （split_diff による逆パッチ適用時に巻き込まれないようにするため）。

        実装方式は問わない:
          - エージェント型（Claude Code 等）: worktree をカレントディレクトリとして起動し、
            自身の Read/Write/Edit ツールで直接ファイルを書き換える。
          - チャット補完API型（素の Anthropic/OpenAI API 等）: プロンプトから diff テキストを
            生成し、このメソッド内で git apply して worktree に適用する。

        呼び出し後、TestGenerator / Validator 側は worktree の `git status --porcelain` /
        ファイル内容を見て「何が追加・変更されたか」を判定する。生成に失敗した場合
        （API エラー・空応答・パース不能な出力等）は例外を送出する。
        """
```

#### 6.1.1 既定実装: `ClaudeCodeAdapter`（`adapters/llm/claude_code.py`）

`claude` CLI（v2.1系で動作確認済み）をサブプロセスとして呼び出す。

```python
subprocess.run(
    [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--permission-prompts", "none",   # 非対話実行なので、プロンプトが必要な呼び出しは自動拒否させる
        "--restricted",                   # Bash等のコード実行系ツールを一旦すべて外し、
                                           # 残るファイル系ツール(Write/Edit/Read等)をcwd配下に閉じ込める
        "--tools", "Bash",                 # --restricted で外された Bash だけ再度ツール一覧に戻す
        "--allowedTools", f"Read,Grep,Glob,Write,Edit,Bash({test_command_prefix} *)",
    ],
    cwd=worktree,
    timeout=...,
    capture_output=True,
    text=True,
)
```

- `--restricted` と `--tools` / `--allowedTools` の役割分担を取り違えないこと。
  `--tools` は「モデルに見せるツールの顔ぶれ」、`--allowedTools` は「確認なしで自動承認する呼び出し」。
  両方を指定して初めて、worktree 内での Read/Write/Edit と、指定コマンドのみの Bash 実行が
  プロンプト待ちにならず動く。**`--restricted` を使うと、`Write`/`Edit` はcwd（worktree）配下にしか
  書き込めなくなることを実機（claude-code 2.1.290）で確認済み**（`--allowedTools` だけで
  `--restricted` を付けない場合、Writeツール自体はcwd外にも書き込めてしまうため、
  このコンボ無しに単に`--allowedTools`を絞るだけでは worktree 外への書き込みを防げない。10.1節参照）。
- `Bash` の許可パターンは `test_command` の先頭トークンから単純に作る
  （例: `test_command="pytest --tb=short -q"` → `Bash(pytest *)`）。`uv run pytest` のように
  ラッパー経由のコマンドだと先頭トークンが広すぎる許可になりうる点は既知の限界とする。
- プロンプトには `TestGenerationTask` の内容（Issue・ソース差分・テストコマンド・既存テストサンプル）を
  埋め込みつつ、「worktree 内のソースを読んで良い／テストファイルは `test_patterns` に沿った場所に
  書くこと／buggy状態を再現するテストを1本書くこと」を指示する。
  Claude Code 自身が Read/Grep/Glob でリポジトリを探索できるため、`sample_test_files` や
  buggy時点のソース内容は厳密には埋め込まなくても自力で見つけられるが、往復（ツール呼び出し）の
  回数とコストを抑えるため、少なくとも Issue 本文とソース差分は最初からプロンプトに含める。
- `--output-format json` の応答には `result`（テキスト）・`is_error`・`modelUsage`
  （モデル名をキーとする使用量辞書）・`permission_denials`・`total_cost_usd` 等が含まれる
  （実機で確認したスキーマ）。成功判定は**プロセスの終了コードではなく `is_error` フィールドで行う**
  （意図的に権限を絞っているため、スコープ外操作が `permission_denials` に載って終了コードが
  非ゼロになることがあるが、それ自体は想定内の拒否であり失敗ではない）。モデル名は
  `modelUsage` のキーから取得し `GeneratedTestInfo.model` に記録する。
- `.env` の `ANTHROPIC_API_KEY` 等、`claude` CLI 自身の認証はユーザー側の既存セットアップ
  （`claude login` 済み、またはAPIキー環境変数）にそのまま乗る。本ツールが別途キー管理を持つ必要はない。

#### 6.1.2 差し替え可能な他実装（将来・任意）

- `AnthropicAPIAdapter`（`adapters/llm/anthropic_api.py`）: 素の Anthropic Messages API を直接叩く。
  `TestGenerationTask` の全フィールドをプロンプトに文字列化して埋め込み、応答から diff を抽出して
  `git apply` する非エージェント型。
- `OpenAIAdapter`（`adapters/llm/openai.py`）: 同様の構造で OpenAI Chat Completions / Responses API を使う。
- いずれも `config.yaml` の `llm.provider` で選択する（7節）。`TestGenerator` / `Validator` 側のコードは
  `LLMAdapter` インターフェースのみに依存するため、provider を増やしても呼び出し側の変更は不要。

### 6.2 コンテキスト組み立て（`core/test_generator.py` 内）

`TestGenerationTask` を組み立てる処理は provider に依存せず共通化する。

- Issueタイトル・本文、`fix.diff` のソース側差分は既に `BugRecord` に保持済みのものをそのまま使う。
- 既存テストのサンプルは `LanguageConfig.test_patterns` でマッチしたファイルを worktree 内から
  1〜2本選んでそのまま埋め込み、フレームワーク・インポート・命名規約を模倣させる
  （エージェント型実装では省略可能な補助情報、非エージェント型実装では必須の情報）。
- 大きなリポジトリ（flask/ripgrep/clap/serde 等サイズの異なるプロジェクトが混在する）を想定し、
  埋め込むファイル単位・プロンプト全体の両方に文字数上限を設け、超過分は切り詰めてその旨を明記する。

### 6.3 検証ループ（`Validator` の拡張）

`Validator` に新規メソッドを追加する。

```python
def validate_with_generated_test(
    self,
    repo_path: Path,
    fix_ref: str,
    source_diff: str,
    llm: LLMAdapter,
    task: TestGenerationTask,
    bug_id: str,
    max_attempts: int,
) -> GenerationAttemptResult:
    """
    LLMAdapter を使って再現テストを生成し、fix:PASS / buggy:FAIL を機械的に確認する。
    確認できたら生成テストのファイル内容・パスを含めて返す。
    """
```

処理内容（1レコードにつき最大 `max_attempts` 回のループを内包する）:
1. 既存の worktree 確保ロジック（永続/一時、`git reset --hard` + `git clean -fd` によるリセット）を再利用し、
   `fix_ref` のスナップショットを用意する。
2. `llm.generate_test(worktree, task, feedback)` を呼ぶ（6.1節の契約どおり、worktree 内に
   テストファイルが書き込まれる）。
3. そのままテスト実行 → `lang_adapter.parse_test_output()` で生成テストケースを名前で引き当て、
   PASS であることを確認する。ここでの失敗（ビルドエラー・テストが見つからない等）は
   record 全体の `setup_failed` にはせず、次の試行へのフィードバックとして扱う。
4. 続けて `source_diff` のみを逆パッチ適用（既存の `reverse_patch_source` と同一戦略。
   生成テストファイルは `test_patterns` に沿ったパスにあるため巻き込まれない）→ テスト実行。
   生成テストが FAIL になっているかを同様に確認する。
5. 3・4 の両方を満たせば confirmed。worktree 内の生成ファイルを読み取って
   `GeneratedTestInfo` を組み立て、呼び出し元に返す。
   満たさなければ、worktree を `fix_ref` に `reset --hard` + `clean -fd` して生成物を消し、
   直前の実行結果（stdout/stderr の該当部分）をフィードバックとして次の試行へ進む。
6. `finally` で worktree を `fix_ref` に `reset --hard` + `clean -fd`
   （既存の `Validator.validate()` の後片付けと同じ）し、永続 worktree に生成テストの痕跡を残さない。

**既存テスト全体への影響は見ない**（他のテストの flaky な挙動は無視し、生成テスト単体の FAIL/PASS だけを見る）。
これが、今回の問題の根本原因である「スイート全体のフォールバック判定に頼らざるを得ない」状況を回避する核心。

### 6.4 `TestGenerator`（オーケストレーター）

`core/test_generator.py` に、`Collector` と同じ粒度で以下を行うクラスを追加する。
試行ループ自体は 6.3 の `Validator.validate_with_generated_test()` に内包されるため、
`TestGenerator` は候補レコードの列挙と結果の保存に専念する薄い層になる。

```
TestGenerator.run(records, llm: LLMAdapter, llm_config) -> GenerationSummary
  for each candidate BugRecord:
    task = build_task(record)  # 6.2節
    result = validator.validate_with_generated_test(
        repo_path, record.fix.commit, source_diff, llm, task,
        record.id, llm_config.max_attempts,
    )
    if result.confirmed:
        record.reproduction.generated_test = result.generated_test
        record.validation = replace(record.validation, status=LLM_VALID)
        storage.save(record)
    else:
        log 失敗(試行回数, 理由)
```

## 7. 設定ファイル拡張

`example-project.yaml` に対応する新ブロック（既存の `validation:` と同じ階層）。

```yaml
llm:
  enabled: false
  provider: claude_code         # LLMAdapter の実装選択。既定は claude_code
  max_attempts: 3
  max_context_chars: 20000      # プロンプトに埋め込むコンテキストの文字数上限

  # provider: claude_code のときのみ参照
  claude_code:
    command: "claude"           # PATH上のclaudeコマンド名、またはフルパス
    allowed_tools: "Read,Grep,Glob,Write,Edit"  # Bash(test_command) は実行時に動的に追加
    timeout: 600

  # provider: anthropic_api / openai_api のときのみ参照（将来実装）
  # model: "claude-sonnet-5"
  # api_key_env: "ANTHROPIC_API_KEY"
```

`provider` を切り替えるだけで `AnthropicAPIAdapter` / `OpenAIAdapter`（6.1.2節、将来実装）にも
差し替えられるようにする。`ProjectConfig` に `LLMConfig` dataclass を追加し、`ValidationConfig` と
同様 `_from_dict` でパースする。`provider` ごとの固有設定（`claude_code:` ブロック等）は
`LLMConfig` 内にネストしたサブdataclassとして持たせ、未対応providerの設定は無視する。

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

### 10.1 Claude Code（`-p`）の権限スコープ

Claude Code は `-p`（非対話）モードでも Read/Write/Edit/Bash 等のツールを使う。対話モードと違い
その場で人間が承認できないため、ヘッドレス実行では事前にツール許可を与える必要がある。
本設計では `--dangerously-skip-permissions` のような全面許可オプションは使わない方針とする。

**`--allowedTools` だけでは worktree 外への書き込みを防げないことを実機（claude-code 2.1.290）で
確認した。** `--allowedTools "Write"` のようにツール名を許可するだけだと、Writeツール自体は
cwd外の任意パスにも書き込めてしまう（実際に `cwd` の親ディレクトリへの書き込みに成功することを
確認した）。worktree への閉じ込めには `--restricted` フラグが必須で、これを付けると
Read/Write/Edit 等のファイル系ツールが cwd（と `--add-dir` で追加したディレクトリ）配下に
確認済みで制限される。`--restricted` は同時に Bash 等のコード実行系ツールを一旦すべて取り除くため、
テスト実行に使う分だけ `--tools` で明示的に戻す。

本設計では次の組み合わせを採用する（6.1.1節）:

- `cwd` は常に使い捨ての worktree とし、本体リポジトリやホームディレクトリには
  Claude Code のカレントディレクトリを向けない。
- `--restricted` を付け、ファイル系ツールを worktree 配下に閉じ込める。
- `--tools "Bash"` で Bash だけを再度ツール一覧に戻し、`--allowedTools` で
  `Read,Grep,Glob,Write,Edit,Bash(<test_commandの先頭トークン> *)` のみを自動承認する。
  任意コマンド実行・ネットワークアクセス・git操作（push/commit等）は許可しない。
- `--permission-prompts none` を付け、スコープ外の呼び出しは（人間の承認を待たず）自動拒否させる。

これにより、万一 LLM が意図しない／悪意に近い出力をしても、影響範囲は使い捨て worktree 内の
ファイルシステム操作とテストコマンド実行に限定される。

### 10.2 一般

- **実行環境**: 生成されたテストコードは信頼できない入力として扱う。実行は常に使い捨ての worktree
  （既存の `Validator` の一時/永続 worktree 機構）の中で行い、開発者の作業コピーには一切触れない。
  `LanguageConfig.test_timeout` を上限として機能させる（既存の仕組みをそのまま適用）。
- **将来課題**: 現状の `subprocess.run(..., shell=True)` ベースのテスト実行自体（Claude Code 経由でない、
  通常の `Validator._run_tests`）はネットワーク／ファイルシステムへのアクセスを制限しない。
  利用規模が拡大する場合はコンテナ等でのサンドボックス実行を追加検討する
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

1. `GeneratedTestInfo` モデル（`provider` フィールド含む）と `llm_valid` ステータス定数を追加する
   （挙動はまだ変えない）。
2. `LLMAdapter` ABC / `TestGenerationTask` と、既定実装 `ClaudeCodeAdapter`（`claude -p` 呼び出し、
   10.1節の権限スコープ）、コンテキスト組み立てを実装する
   （検証ループなし。生成結果をログ出力して目視確認できる段階に留める）。
3. `Validator.validate_with_generated_test()` と再試行・フィードバックループを実装する
   （`ClaudeCodeAdapter` で動作確認）。
4. `bp generate-tests` CLIコマンドと `bp stats` の内訳表示更新を実装する。
5. （フォローアップ、任意）`AnthropicAPIAdapter` / `OpenAIAdapter`（6.1.2節）を追加し、
   `llm.provider` 切り替えで他のLLM/APIも使えることを確認する。
6. （フォローアップ）利用規模の拡大に応じたサンドボックス実行の強化を検討する。
