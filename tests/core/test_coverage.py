from __future__ import annotations

from pathlib import Path

import pytest

from bug_prospector.adapters.coverage.base import CoverageAdapter, FileCoverage, ParsedCoverage
from bug_prospector.config import CoverageConfig
from bug_prospector.core.coverage import CoverageRunner, _diff_coverage, _diff_removed_lines
from bug_prospector.core.models.record import COVERAGE_ERROR, MEASURED, UNSUPPORTED

DIFF = """diff --git a/src/foo.rs b/src/foo.rs
index 1111111..2222222 100644
--- a/src/foo.rs
+++ b/src/foo.rs
@@ -10,7 +10,7 @@ fn bar() {
 fn bar() {
-    let x = 1;
-    let y = 2;
+    let x = 10;
+    let y = 20;
     x + y
 }
"""


class _FakeAdapter(CoverageAdapter):
    def __init__(self, parsed: ParsedCoverage, report_name: str = "report.xml") -> None:
        self._parsed = parsed
        self._report_name = report_name

    @property
    def tool_name(self) -> str:
        return "fake-tool"

    def default_command(self, repo_path: Path) -> str:
        return f"echo hi > {self._report_name}"

    def default_report_path(self, repo_path: Path) -> Path:
        return repo_path / self._report_name

    def parse_report(self, report_path: Path) -> ParsedCoverage:
        return self._parsed


class TestDiffRemovedLines:
    def test_removed_lines_tracked(self):
        result = _diff_removed_lines(DIFF)
        assert result["src/foo.rs"] == {11, 12}

    def test_no_diff_header_ignored(self):
        assert _diff_removed_lines("") == {}

    def test_added_only_hunk_has_no_removed_lines(self):
        diff = (
            "diff --git a/src/foo.rs b/src/foo.rs\n"
            "--- a/src/foo.rs\n"
            "+++ b/src/foo.rs\n"
            "@@ -5,2 +5,3 @@\n"
            " context\n"
            "+new line\n"
            " more context\n"
        )
        assert _diff_removed_lines(diff) == {}


class TestDiffCoverage:
    def test_touched_covered_and_total(self):
        fc = FileCoverage(path="src/foo.rs", instrumented_lines={11, 12}, covered_lines={11})
        total, covered = _diff_coverage(DIFF, {"src/foo.rs": fc})
        assert total == 2
        assert covered == 1

    def test_file_not_in_report_contributes_zero(self):
        total, covered = _diff_coverage(DIFF, {})
        assert (total, covered) == (0, 0)

    def test_non_instrumented_lines_excluded_from_total(self):
        fc = FileCoverage(path="src/foo.rs", instrumented_lines={11}, covered_lines={11})
        total, covered = _diff_coverage(DIFF, {"src/foo.rs": fc})
        assert total == 1
        assert covered == 1

    def test_suffix_match_for_jacoco_style_paths(self):
        # JaCoCo は "com/example/Foo.java" のようにルート相対でないパスを返す
        diff = (
            "diff --git a/src/main/java/com/example/Foo.java b/src/main/java/com/example/Foo.java\n"
            "--- a/src/main/java/com/example/Foo.java\n"
            "+++ b/src/main/java/com/example/Foo.java\n"
            "@@ -3,1 +3,1 @@\n"
            "-old\n"
            "+new\n"
        )
        fc = FileCoverage(path="com/example/Foo.java", instrumented_lines={3}, covered_lines=set())
        total, covered = _diff_coverage(diff, {"com/example/Foo.java": fc})
        assert total == 1
        assert covered == 0


class TestCoverageRunnerMeasure:
    def test_disabled_returns_unsupported(self, tmp_path):
        runner = CoverageRunner(adapter=_FakeAdapter(ParsedCoverage()), config=CoverageConfig(enabled=False))
        result = runner.measure(tmp_path, DIFF)
        assert result.status == UNSUPPORTED

    def test_no_adapter_returns_unsupported(self, tmp_path):
        runner = CoverageRunner(adapter=None, config=CoverageConfig(enabled=True))
        result = runner.measure(tmp_path, DIFF)
        assert result.status == UNSUPPORTED

    def test_report_missing_returns_error(self, tmp_path):
        adapter = _FakeAdapter(ParsedCoverage(), report_name="never-created.xml")
        runner = CoverageRunner(
            adapter=adapter,
            config=CoverageConfig(enabled=True, command="true"),
        )
        result = runner.measure(tmp_path, DIFF)
        assert result.status == COVERAGE_ERROR

    def test_success_computes_diff_coverage(self, tmp_path):
        (tmp_path / "report.xml").write_text("", encoding="utf-8")
        fc = FileCoverage(path="src/foo.rs", instrumented_lines={11, 12}, covered_lines={11})
        parsed = ParsedCoverage(files={"src/foo.rs": fc}, overall_line_rate=0.8, overall_branch_rate=0.6)
        adapter = _FakeAdapter(parsed)
        runner = CoverageRunner(adapter=adapter, config=CoverageConfig(enabled=True, command="true"))

        result = runner.measure(tmp_path, DIFF)

        assert result.status == MEASURED
        assert result.tool == "fake-tool"
        assert result.overall_line_rate == 0.8
        assert result.overall_branch_rate == 0.6
        assert result.diff_lines_total == 2
        assert result.diff_lines_covered == 1
        assert result.diff_coverage_rate == pytest.approx(0.5)
