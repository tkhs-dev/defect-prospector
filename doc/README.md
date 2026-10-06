# 設計書一覧 — defect-prospector

実際の使い方（インストール・CLI・設定ファイル・パイプラインの挙動）は
[../README.md](../README.md) を一次情報として参照すること。以下は設計時点の
ドキュメント群で、一部は実装と差異がある（各ファイルの注記を参照）。

| ドキュメント | 概要 |
|---|---|
| [requirements.md](requirements.md) | 機能要求・非機能要求の定義（設計初期のもの） |
| [architecture.md](architecture.md) | システム全体構成・レイヤー設計・データフロー（設計初期のもの。パッケージ構成は実装と一部異なる） |
| [data-schema.md](data-schema.md) | BugRecordのデータモデル定義・ストレージ形式 |
| [adapter-interface.md](adapter-interface.md) | VCS / IssueTracker / Language / Coverage アダプタのABCと拡張方法 |
| [cli-spec.md](cli-spec.md) | CLIコマンド仕様（設計初期のもの。末尾に実装との差分注記あり） |
| [test-rigor-comparison.md](test-rigor-comparison.md) | Rust/Java/Pythonのテスト厳密さ比較の実験設計・対象プロジェクトの選定理由・実行手順 |
| [test-generation.md](test-generation.md) | テスト非同梱バグに対するLLMベース再現テスト生成の設計・`bp generate-tests`コマンド |
| [rug-paper-summary.md](rug-paper-summary.md) | RUG論文（Rust向けLLMテスト生成＋ファジング）の要約 |
| [rug-integration-note.md](rug-integration-note.md) | RUGのdefect-prospector統合スコープ検討メモ（**未決定・作業再開用**） |
