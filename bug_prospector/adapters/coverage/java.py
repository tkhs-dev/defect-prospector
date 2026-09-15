from __future__ import annotations

from pathlib import Path

from .base import CoverageAdapter, ParsedCoverage
from .jacoco import parse_jacoco


class JacocoAdapter(CoverageAdapter):

    @property
    def tool_name(self) -> str:
        return "jacoco"

    # プラグインをpomに追加せず ad hoc に実行するための完全なGAV座標。
    _MAVEN_JACOCO_GAV = "org.jacoco:jacoco-maven-plugin:0.8.12"

    def default_command(self, repo_path: Path) -> str:
        if (repo_path / "pom.xml").exists():
            # prepare-agent でJavaエージェントを付与してからtestを実行しないと
            # jacoco.exec が生成されず report が空になる。
            return (
                f"mvn {self._MAVEN_JACOCO_GAV}:prepare-agent "
                f"test "
                f"{self._MAVEN_JACOCO_GAV}:report -B"
            )
        if (repo_path / "gradlew").exists():
            return "./gradlew test jacocoTestReport --console=plain"
        return "gradle test jacocoTestReport --console=plain"

    def default_report_path(self, repo_path: Path) -> Path:
        if (repo_path / "pom.xml").exists():
            return repo_path / "target" / "site" / "jacoco" / "jacoco.xml"
        return repo_path / "build" / "reports" / "jacoco" / "test" / "jacocoTestReport.xml"

    def parse_report(self, report_path: Path) -> ParsedCoverage:
        return parse_jacoco(report_path)
