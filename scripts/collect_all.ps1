# Rust vs Java テスト厳格さ比較 - 全プロジェクト一括収集 (Windows / PowerShell)
# 実験設計・対象プロジェクトは doc/rust-vs-java-testing.md を参照。
#
# 使い方:
#   .\scripts\collect_all.ps1 -NoValidation         # 静的先行測定（ビルド・テスト実行なし。推奨: 最初に実行）
#   .\scripts\collect_all.ps1                       # 各yamlの validation/coverage 設定どおりに収集
#   .\scripts\collect_all.ps1 -Limit 10             # プロジェクトあたり10件だけ試験収集
#   .\scripts\collect_all.ps1 -DryRun               # 何も保存せずプレビューのみ
#   .\scripts\collect_all.ps1 -Only clap,picocli    # 特定プロジェクトだけ実行
#
# 前提:
#   - .env に GITHUB_TOKEN が設定済みであること
#   - -NoValidation なしで実行する場合のみ、各言語のツールチェインが必要:
#     - Rust側: cargo（coverage.enabled: true のプロジェクトがあれば `cargo install cargo-llvm-cov` も）
#     - Java側: JDK と Maven（Gradleプロジェクトは同梱の gradlew を使う）
#
# 各プロジェクトの収集は独立している。1件失敗しても後続のプロジェクトは実行される。

param(
    [int]$Limit = 0,
    [switch]$DryRun,
    [switch]$NoValidation,
    [string]$DatasetDir = "./dataset",
    [string[]]$Only = @()
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# 3章: ドメインペア（Rust / Java）
# 3.1節: 静的指標の母数拡大用の追加プロジェクト
$projects = [ordered]@{
    "clap"       = "clap.yaml"
    "picocli"    = "projects/picocli.yaml"
    "serde_json" = "projects/serde_json.yaml"
    "gson"       = "gson.yaml"
    "hyper"      = "projects/hyper.yaml"
    "vertx"      = "projects/vertx.yaml"
    "ripgrep"    = "ripgrep.yaml"
    "serde"      = "serde.yaml"
    "tokio"      = "projects/tokio.yaml"
    "jsoup"      = "jsoup.yaml"
    "log4j2"     = "log4j2.yaml"
    "netty"      = "projects/netty.yaml"
}

$failures = @()

foreach ($name in $projects.Keys) {
    if ($Only.Count -gt 0 -and ($Only -notcontains $name)) {
        continue
    }
    $path = $projects[$name]

    Write-Host ""
    Write-Host "=== bp collect --project $path ===" -ForegroundColor Cyan

    $cliArgs = @("collect", "--project", $path)
    if ($Limit -gt 0) { $cliArgs += @("--limit", "$Limit") }
    if ($NoValidation) { $cliArgs += "--no-validation" }
    if ($DryRun) { $cliArgs += "--dry-run" }

    & bp --dataset-dir $DatasetDir @cliArgs
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "collect failed for $name (exit $LASTEXITCODE) - continuing with next project"
        $failures += $name
    }
}

Write-Host ""
if ($failures.Count -gt 0) {
    Write-Warning "Projects with failures: $($failures -join ', ')"
}
Write-Host "Done. Run 'bp --dataset-dir $DatasetDir compare' to see the rust vs java comparison." -ForegroundColor Green
