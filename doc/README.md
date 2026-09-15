# 設計書一覧 — defect-prospector

| ドキュメント | 概要 |
|---|---|
| [requirements.md](requirements.md) | 機能要求・非機能要求の定義 |
| [architecture.md](architecture.md) | システム全体構成・レイヤー設計・データフロー |
| [data-schema.md](data-schema.md) | BugRecordのデータモデル定義・ストレージ形式 |
| [adapter-interface.md](adapter-interface.md) | VCS / IssueTracker / Language アダプタのABCと拡張方法 |
| [cli-spec.md](cli-spec.md) | CLIコマンド仕様（`bp collect` / `checkout` 等） |
| [test-generation.md](test-generation.md) | LLMによるテスト自動生成の設計（生成トリガー・既存テストとの関係・検証パイプライン） |
| [rust-vs-java-testing.md](rust-vs-java-testing.md) | Rust/Javaのテスト厳格さ比較の実験設計・対象プロジェクト・実行手順 |