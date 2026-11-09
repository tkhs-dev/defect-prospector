#!/usr/bin/env bash
# 収集済みデータセットから Rust vs Java 比較レポートを生成する (Linux / macOS / Git Bash)
#
# 使い方:
#   ./scripts/report.sh
#   ./scripts/report.sh --dataset-dir ./dataset --out-dir ./dataset/reports

set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

DATASET_DIR="./dataset"
OUT_DIR="./dataset/reports"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dataset-dir) DATASET_DIR="$2"; shift 2 ;;
    --out-dir) OUT_DIR="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

mkdir -p "$OUT_DIR"

echo "=== bp stats ==="
bp --dataset-dir "$DATASET_DIR" stats | tee "$OUT_DIR/stats.txt"
bp --dataset-dir "$DATASET_DIR" stats --json > "$OUT_DIR/stats.json"

echo
echo "=== bp compare (rust vs java) ==="
bp --dataset-dir "$DATASET_DIR" compare | tee "$OUT_DIR/compare.txt"
bp --dataset-dir "$DATASET_DIR" compare --json > "$OUT_DIR/compare.json"

echo
echo "Reports written to $OUT_DIR"
