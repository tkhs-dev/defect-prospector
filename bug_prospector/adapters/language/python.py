from __future__ import annotations

import re
from pathlib import Path

from .base import LanguageAdapter
from ...core.models.record import TestCaseResult


class PythonAdapter(LanguageAdapter):

    @property
    def language_name(self) -> str:
        return "python"

    def detect(self, repo_path: Path) -> bool:
        indicators = ["pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"]
        return any((repo_path / f).exists() for f in indicators)

    def default_test_patterns(self) -> list[str]:
        return ["tests/**", "test/**", "test_*.py", "*_test.py"]

    def default_test_command(self, repo_path: Path) -> str:
        if (repo_path / "pytest.ini").exists() or (repo_path / "pyproject.toml").exists():
            return "pytest --tb=short -q"
        return "python -m pytest --tb=short -q"

    def default_setup_commands(self, repo_path: Path) -> list[str]:
        if (repo_path / "pyproject.toml").exists():
            return ["pip install -e '.[dev,test]' --quiet 2>/dev/null || pip install -e . --quiet"]
        if (repo_path / "requirements.txt").exists():
            return ["pip install -r requirements.txt --quiet"]
        return []

    def parse_test_output(self, stdout: str, stderr: str) -> list[TestCaseResult]:
        """pytest の出力からテストケース単体の結果を抽出する。

        verbose (-v): "tests/foo.py::test_name PASSED/FAILED [ N%]"
        quiet   (-q): 失敗行のみ "FAILED tests/foo.py::test_name - msg"
        """
        results: list[TestCaseResult] = []
        combined = stdout + "\n" + stderr

        for line in combined.splitlines():
            # verbose: "tests/foo.py::test_name PASSED" or "FAILED"
            m = re.match(r'^(\S+::[\w\[\]-]+)\s+(PASSED|FAILED)(?:\s|$)', line)
            if m:
                results.append(TestCaseResult(name=m.group(1), passed=m.group(2) == "PASSED"))
                continue

            # quiet: "FAILED tests/foo.py::test_name - AssertionError: ..."
            m = re.match(r'^FAILED (\S+::[\w\[\]-]+)\s*(?:-\s*(.+))?$', line)
            if m:
                results.append(TestCaseResult(
                    name=m.group(1),
                    passed=False,
                    message=(m.group(2) or "").strip() or None,
                ))

        return results
