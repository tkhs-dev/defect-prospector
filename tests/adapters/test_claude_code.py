from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bug_prospector.adapters.llm.base import TestGenerationTask
from bug_prospector.adapters.llm.claude_code import ClaudeCodeAdapter
from bug_prospector.config import ClaudeCodeConfig


def _task(test_command: str = "pytest -q", language: str = "python") -> TestGenerationTask:
    return TestGenerationTask(
        issue_title="Crashes on empty input",
        issue_body="Calling foo('') raises IndexError",
        source_diff="diff --git a/src/foo.py b/src/foo.py\n...",
        test_patterns=["tests/**"],
        language=language,
        test_command=test_command,
    )


def _completed(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


class TestCommandConstruction:
    def test_includes_restricted_and_permission_flags(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _completed(json.dumps({"is_error": False, "result": "ok", "modelUsage": {}}))

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        adapter.generate_test(tmp_path, _task())

        cmd = captured["cmd"]
        assert cmd[0] == "claude"
        assert "-p" in cmd
        assert "--restricted" in cmd
        assert "--permission-prompts" in cmd and "none" in cmd
        assert captured["kwargs"]["cwd"] == tmp_path

    def test_bash_pattern_derived_from_test_command_prefix(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(json.dumps({"is_error": False, "result": "ok", "modelUsage": {}}))

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        adapter.generate_test(tmp_path, _task(test_command="cargo test"))

        allowed_tools_idx = captured["cmd"].index("--allowedTools") + 1
        assert "Bash(cargo *)" in captured["cmd"][allowed_tools_idx]

    def test_rust_task_gets_bottom_up_addendum_and_cargo_check_permission(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(json.dumps({"is_error": False, "result": "ok", "modelUsage": {}}))

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        # test_command がラッパー経由(cargo以外が先頭)でも、rustなら cargo check は常に許可される
        adapter.generate_test(tmp_path, _task(test_command="just test", language="rust"))

        prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
        assert "build bottom-up, verify as you go" in prompt
        assert "cargo check" in prompt

        allowed_tools = captured["cmd"][captured["cmd"].index("--allowedTools") + 1]
        assert "Bash(cargo *)" in allowed_tools
        assert "Bash(just *)" in allowed_tools

    def test_non_rust_task_has_no_bottom_up_addendum_or_cargo_permission(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(json.dumps({"is_error": False, "result": "ok", "modelUsage": {}}))

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        adapter.generate_test(tmp_path, _task(test_command="pytest -q", language="python"))

        prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
        assert "build bottom-up, verify as you go" not in prompt

        allowed_tools = captured["cmd"][captured["cmd"].index("--allowedTools") + 1]
        assert "Bash(cargo *)" not in allowed_tools

    def test_rust_bottom_up_disabled_gives_one_shot_prompt(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(json.dumps({"is_error": False, "result": "ok", "modelUsage": {}}))

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig(rust_bottom_up=False))
        adapter.generate_test(tmp_path, _task(test_command="cargo test", language="rust"))

        prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
        assert "build bottom-up, verify as you go" not in prompt
        # プロンプト以外の条件（ツール・権限）は揃える
        allowed_tools = captured["cmd"][captured["cmd"].index("--allowedTools") + 1]
        assert "Bash(cargo *)" in allowed_tools

    def test_feedback_is_appended_to_prompt(self, monkeypatch, tmp_path):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(json.dumps({"is_error": False, "result": "ok", "modelUsage": {}}))

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        adapter.generate_test(tmp_path, _task(), feedback="the test did not fail as expected")

        prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
        assert "the test did not fail as expected" in prompt


class TestResultHandling:
    def test_extracts_model_from_model_usage(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: _completed(json.dumps({
                "is_error": False, "result": "ok",
                "modelUsage": {"claude-opus-5": {"inputTokens": 1}},
            })),
        )
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        model = adapter.generate_test(tmp_path, _task())
        assert model == "claude-opus-5"

    def test_raises_on_is_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: _completed(json.dumps({"is_error": True, "result": "failed to comply"})),
        )
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        with pytest.raises(RuntimeError, match="failed to comply"):
            adapter.generate_test(tmp_path, _task())

    def test_raises_on_unparseable_output(self, monkeypatch, tmp_path):
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed("not json", returncode=1))
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        with pytest.raises(RuntimeError):
            adapter.generate_test(tmp_path, _task())

    def test_raises_on_timeout(self, monkeypatch, tmp_path):
        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=1)

        monkeypatch.setattr(subprocess, "run", fake_run)
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig(timeout=1))
        with pytest.raises(RuntimeError, match="timed out"):
            adapter.generate_test(tmp_path, _task())


def _screening_output(verdict: dict | None, result: str = "") -> str:
    data = {"is_error": False, "result": result, "modelUsage": {"claude-opus-5-5": {}}}
    if verdict is not None:
        data["structured_output"] = verdict
    return json.dumps(data)


class TestAssessTestability:
    def test_runs_without_tools_and_with_json_schema(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(_screening_output({"testable": True, "category": "program_bug", "reason": "r"}))

        monkeypatch.setattr(subprocess, "run", fake_run)
        ClaudeCodeAdapter(ClaudeCodeConfig()).assess_testability(_task(language="rust", test_command="cargo test"))

        cmd = captured["cmd"]
        assert cmd[cmd.index("--tools") + 1] == ""
        assert "--allowedTools" not in cmd
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        assert schema["required"] == ["testable", "category", "reason"]
        prompt = cmd[cmd.index("-p") + 1]
        assert "Crashes on empty input" in prompt
        assert "cargo test" in prompt

    def test_parses_structured_output(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: _completed(_screening_output(
                {"testable": False, "category": "not_program_bug", "reason": "docs only"})),
        )
        verdict = ClaudeCodeAdapter(ClaudeCodeConfig()).assess_testability(_task())
        assert verdict.testable is False
        assert verdict.category == "not_program_bug"
        assert verdict.reason == "docs only"
        assert verdict.model == "claude-opus-5-5"

    def test_falls_back_to_json_in_result_text(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: _completed(_screening_output(
                None, result='{"testable": true, "category": "program_bug", "reason": "logic"}')),
        )
        assert ClaudeCodeAdapter(ClaudeCodeConfig()).assess_testability(_task()).testable is True

    def test_raises_without_verdict(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: _completed(_screening_output(None, "I am not sure")))
        with pytest.raises(RuntimeError, match="no usable screen verdict"):
            ClaudeCodeAdapter(ClaudeCodeConfig()).assess_testability(_task())

    def test_usage_is_recorded_with_kind(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: _completed(_screening_output({"testable": True, "category": "program_bug", "reason": ""})),
        )
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        adapter.assess_testability(_task())
        assert adapter.usage_records[-1]["kind"] == "screen"


class TestClassifyBug:
    def test_prompt_includes_fix_title_and_schema_has_categories(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(_screening_output({"is_bug": True, "category": "bug", "reason": "r"}))

        monkeypatch.setattr(subprocess, "run", fake_run)
        task = _task()
        task.fix_title = "Fix crash on empty input"
        verdict = ClaudeCodeAdapter(ClaudeCodeConfig()).classify_bug(task)

        assert verdict.is_bug is True
        cmd = captured["cmd"]
        assert cmd[cmd.index("--tools") + 1] == ""
        prompt = cmd[cmd.index("-p") + 1]
        assert "Fix crash on empty input" in prompt
        assert "Crashes on empty input" in prompt
        schema = json.loads(cmd[cmd.index("--json-schema") + 1])
        assert "feature_request" in schema["properties"]["category"]["enum"]

    def test_parses_negative_verdict_and_records_kind(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: _completed(_screening_output(
                {"is_bug": False, "category": "question", "reason": "usage question"})),
        )
        adapter = ClaudeCodeAdapter(ClaudeCodeConfig())
        verdict = adapter.classify_bug(_task())

        assert (verdict.is_bug, verdict.category, verdict.reason) == (False, "question", "usage question")
        assert adapter.usage_records[-1]["kind"] == "classify"

    def test_raises_when_decision_missing(self, monkeypatch):
        monkeypatch.setattr(
            subprocess, "run",
            lambda cmd, **kw: _completed(_screening_output({"testable": True, "category": "bug", "reason": ""})),
        )
        with pytest.raises(RuntimeError, match="no usable classify verdict"):
            ClaudeCodeAdapter(ClaudeCodeConfig()).classify_bug(_task())

    @pytest.mark.parametrize("kind, heading, origin", [
        ("pull_request", "Pull request description", "through a pull request"),
        ("commit", "Commit message", "as a direct commit"),
    ])
    def test_fix_only_prompt_for_pr_and_commit_sources(self, monkeypatch, kind, heading, origin):
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _completed(_screening_output({"is_bug": True, "category": "bug", "reason": ""}))

        monkeypatch.setattr(subprocess, "run", fake_run)
        task = _task()
        task.source_kind = kind
        ClaudeCodeAdapter(ClaudeCodeConfig()).classify_bug(task)

        prompt = captured["cmd"][captured["cmd"].index("-p") + 1]
        assert heading in prompt and origin in prompt
        assert "NO separate bug report" in prompt
        assert "## Issue" not in prompt
