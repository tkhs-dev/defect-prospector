from __future__ import annotations

from pathlib import Path

from .base import LanguageAdapter


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
