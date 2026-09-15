from __future__ import annotations

from bug_prospector.adapters.coverage.rust import CargoLlvmCovAdapter


def test_default_command_includes_workspace_flag(tmp_path):
    """--workspace が無いとワークスペースのルートパッケージしか計装されず、
    実装本体を別クレートに置くプロジェクト（例: clap の clap_builder）の
    カバレッジが丸ごと欠落する。"""
    command = CargoLlvmCovAdapter().default_command(tmp_path)
    assert "--workspace" in command.split()


def test_default_report_path(tmp_path):
    adapter = CargoLlvmCovAdapter()
    assert adapter.default_report_path(tmp_path) == tmp_path / "target" / "llvm-cov-cobertura.xml"
