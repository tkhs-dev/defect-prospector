from __future__ import annotations

import re
from pathlib import Path

from .base import LanguageAdapter
from ...core.models.record import TestCaseResult


class JavaAdapter(LanguageAdapter):

    @property
    def language_name(self) -> str:
        return "java"

    def detect(self, repo_path: Path) -> bool:
        return any(
            (repo_path / f).exists()
            for f in ("pom.xml", "build.gradle", "build.gradle.kts")
        )

    def default_test_patterns(self) -> list[str]:
        return ["src/test/**", "*Test.java", "Test*.java", "*Tests.java", "*IT.java"]

    def default_test_command(self, repo_path: Path) -> str:
        if (repo_path / "pom.xml").exists():
            return "mvn test -B"
        if (repo_path / "gradlew").exists():
            return "./gradlew test --console=plain"
        return "gradle test --console=plain"

    def default_setup_commands(self, repo_path: Path) -> list[str]:
        if (repo_path / "pom.xml").exists():
            return ["mvn -q -B compile test-compile"]
        if (repo_path / "gradlew").exists():
            return ["./gradlew compileTestJava --console=plain -q"]
        if (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists():
            return ["gradle compileTestJava --console=plain -q"]
        return []

    def parse_test_output(self, stdout: str, stderr: str) -> list[TestCaseResult]:
        """Maven Surefire / Gradle test の出力からテストケース単体の結果を抽出する。

        Gradle (デフォルト設定): "ClassName > methodName() PASSED/FAILED/SKIPPED"
        Maven Surefire: 成功したテストの個別名はコンソールに出ないため、
                        "Failed tests:" / "Tests in error:" セクションに列挙される
                        "ClassName.methodName:LINE ..." / "ClassName.methodName » Error" 形式
                        の失敗ケースのみ抽出する。
        """
        results: list[TestCaseResult] = []
        seen: set[str] = set()
        combined = stdout + "\n" + stderr
        lines = combined.splitlines()

        # --- Gradle: "ClassName > methodName() STATUS" ---
        gradle_re = re.compile(r'^(\S+) > (.+?) (PASSED|FAILED|SKIPPED)\s*$')
        for line in lines:
            m = gradle_re.match(line.strip())
            if not m:
                continue
            cls, method, status = m.group(1), m.group(2), m.group(3)
            if status == "SKIPPED":
                continue
            name = f"{cls}.{method}"
            if name in seen:
                continue
            seen.add(name)
            results.append(TestCaseResult(name=name, passed=status == "PASSED"))

        # --- Maven Surefire: "Failed tests:" / "Tests in error:" セクション ---
        section_re = re.compile(r'^(Failed tests|Tests in error):\s*$')
        entry_re = re.compile(r'^\s*([\w$]+(?:\.[\w$]+)+)(?::\d+|\s*»)')
        in_section = False
        for line in lines:
            if section_re.match(line):
                in_section = True
                continue
            if not in_section:
                continue
            if not line.strip():
                in_section = False
                continue
            m = entry_re.match(line)
            if not m:
                in_section = False
                continue
            name = m.group(1)
            if name not in seen:
                seen.add(name)
                results.append(TestCaseResult(name=name, passed=False))

        return results
