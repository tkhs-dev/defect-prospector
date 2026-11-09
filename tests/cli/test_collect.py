from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from bug_prospector.cli.main import cli

_YAML = """\
project_id: "example"
repo:
  url: https://github.com/example/repo
tracker:
  type: github
  url: https://github.com/example/repo
  auth:
    token: dummy
language:
  type: rust
validation:
  enabled: true
coverage:
  enabled: true
"""


def _run_collect(tmp_path: Path, monkeypatch, *extra: str):
    captured = {}

    def fake_run_project(self, project, limit_override=None, dry_run=False):
        captured["project"] = project
        return 0, 0, 0, {}

    monkeypatch.setattr("bug_prospector.core.collector.Collector.run_project", fake_run_project)
    project_file = tmp_path / "project.yaml"
    project_file.write_text(_YAML, encoding="utf-8")
    result = CliRunner().invoke(
        cli, ["--dataset-dir", str(tmp_path / "dataset"), "collect", "--project", str(project_file), *extra],
    )
    assert result.exit_code == 0, result.output
    return captured["project"]


def test_no_validation_disables_validation_and_coverage(tmp_path, monkeypatch):
    project = _run_collect(tmp_path, monkeypatch, "--no-validation")
    assert project.validation.enabled is False
    assert project.coverage.enabled is False


def test_without_flag_keeps_yaml_settings(tmp_path, monkeypatch):
    project = _run_collect(tmp_path, monkeypatch)
    assert project.validation.enabled is True
    assert project.coverage.enabled is True
