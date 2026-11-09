# 収集済みデータセットから Rust vs Java 比較レポートを生成する (Windows / PowerShell)
#
# 使い方:
#   .\scripts\report.ps1
#   .\scripts\report.ps1 -DatasetDir ./dataset -OutDir ./dataset/reports

param(
    [string]$DatasetDir = "./dataset",
    [string]$OutDir = "./dataset/reports"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

Write-Host "=== bp stats ===" -ForegroundColor Cyan
& bp --dataset-dir $DatasetDir stats | Tee-Object -FilePath (Join-Path $OutDir "stats.txt")
& bp --dataset-dir $DatasetDir stats --json | Out-File -Encoding utf8 (Join-Path $OutDir "stats.json")

Write-Host ""
Write-Host "=== bp compare (rust vs java) ===" -ForegroundColor Cyan
& bp --dataset-dir $DatasetDir compare | Tee-Object -FilePath (Join-Path $OutDir "compare.txt")
& bp --dataset-dir $DatasetDir compare --json | Out-File -Encoding utf8 (Join-Path $OutDir "compare.json")

Write-Host ""
Write-Host "Reports written to $OutDir" -ForegroundColor Green
