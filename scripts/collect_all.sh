#!/usr/bin/env bash
# Rust vs Java テスト厳格さ比較 - 全プロジェクト一括収集 (Linux / macOS / Git Bash)
# 実験設計・対象プロジェクトは doc/rust-vs-java-testing.md を参照。
#
# 使い方:
#   ./scripts/collect_all.sh --no-validation        # 静的先行測定（ビルド・テスト実行なし。推奨: 最初に実行）
#   ./scripts/collect_all.sh                        # 各yamlの validation/coverage 設定どおりに収集
#   ./scripts/collect_all.sh --limit 10             # プロジェクトあたり10件だけ試験収集
#   ./scripts/collect_all.sh --dry-run              # 何も保存せずプレビューのみ
#   ./scripts/collect_all.sh --only clap,picocli    # 特定プロジェクトだけ実行
#
# 前提:
#   - .env に GITHUB_TOKEN が設定済みであること
#   - --no-validation なしで実行する場合のみ、各言語のツールチェインが必要:
#     - Rust側: cargo（coverage.enabled: true のプロジェクトがあれば `cargo install cargo-llvm-cov` も）
#     - Java側: JDK と Maven
#   - 以下の yaml はコマンドが Windows (cmd.exe) 向けのため、POSIX環境でテスト実行する場合は書き換えること
#     （--no-validation ならコマンドは実行されないので影響しない）:
#     - projects/picocli.yaml: "gradlew.bat" → "./gradlew"
#     - gson.yaml: `set "JAVA_HOME=..." &&` → `JAVA_HOME=... ` 形式
#
# 各プロジェクトの収集は独立している。1件失敗しても後続のプロジェクトは実行される。

set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

LIMIT=""
DRY_RUN=""
NO_VALIDATION=""
DATASET_DIR="./dataset"
ONLY=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --limit) LIMIT="$2"; shift 2 ;;
    --dry-run) DRY_RUN="--dry-run"; shift ;;
    --no-validation) NO_VALIDATION="--no-validation"; shift ;;
    --dataset-dir) DATASET_DIR="$2"; shift 2 ;;
    --only) ONLY="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

# 3章: ドメインペア（Rust / Java）、3.1節: 静的指標の母数拡大用の追加プロジェクト
declare -A projects=(
  [clap]="clap.yaml"
  [picocli]="projects/picocli.yaml"
  [serde_json]="projects/serde_json.yaml"
  [gson]="gson.yaml"
  [hyper]="projects/hyper.yaml"
  [vertx]="projects/vertx.yaml"
  [ripgrep]="ripgrep.yaml"
  [serde]="serde.yaml"
  [tokio]="projects/tokio.yaml"
  [jsoup]="jsoup.yaml"
  [log4j2]="log4j2.yaml"
  [netty]="projects/netty.yaml"
)
order=(clap picocli serde_json gson hyper vertx ripgrep serde tokio jsoup log4j2 netty)

failures=()

is_selected() {
  [[ -z "$ONLY" ]] && return 0
  IFS=',' read -ra sel <<< "$ONLY"
  for s in "${sel[@]}"; do
    [[ "$s" == "$1" ]] && return 0
  done
  return 1
}

for name in "${order[@]}"; do
  is_selected "$name" || continue
  path="${projects[$name]}"

  echo
  echo "=== bp collect --project $path ==="

  args=(collect --project "$path")
  [[ -n "$LIMIT" ]] && args+=(--limit "$LIMIT")
  [[ -n "$NO_VALIDATION" ]] && args+=("$NO_VALIDATION")
  [[ -n "$DRY_RUN" ]] && args+=("$DRY_RUN")

  if ! bp --dataset-dir "$DATASET_DIR" "${args[@]}"; then
    echo "WARNING: collect failed for $name - continuing with next project" >&2
    failures+=("$name")
  fi
done

echo
if [[ ${#failures[@]} -gt 0 ]]; then
  echo "WARNING: projects with failures: ${failures[*]}" >&2
fi
echo "Done. Run 'bp --dataset-dir $DATASET_DIR compare' to see the rust vs java comparison."
