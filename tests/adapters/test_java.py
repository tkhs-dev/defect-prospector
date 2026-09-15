from __future__ import annotations

import pytest

from bug_prospector.adapters.language.java import JavaAdapter


@pytest.fixture
def adapter() -> JavaAdapter:
    return JavaAdapter()


class TestGradleOutput:
    def test_pass(self, adapter):
        results = adapter.parse_test_output("FooTest > bar() PASSED\n", "")
        assert len(results) == 1
        assert results[0].name == "FooTest.bar()"
        assert results[0].passed is True

    def test_fail(self, adapter):
        results = adapter.parse_test_output("FooTest > bar() FAILED\n", "")
        assert len(results) == 1
        assert results[0].name == "FooTest.bar()"
        assert results[0].passed is False

    def test_skipped_excluded(self, adapter):
        results = adapter.parse_test_output("FooTest > bar() SKIPPED\n", "")
        assert results == []

    def test_mixed_pass_fail(self, adapter):
        out = (
            "FooTest > pass1() PASSED\n"
            "FooTest > fail1() FAILED\n"
            "    org.opentest4j.AssertionFailedError at FooTest.java:15\n"
            "BarTest > pass2() PASSED\n"
        )
        results = adapter.parse_test_output(out, "")
        assert len(results) == 3
        assert sum(1 for r in results if r.passed) == 2
        assert sum(1 for r in results if not r.passed) == 1

    def test_no_duplicate_names(self, adapter):
        out = "FooTest > bar() FAILED\nFooTest > bar() FAILED\n"
        results = adapter.parse_test_output(out, "")
        assert len(results) == 1

    def test_empty_output(self, adapter):
        assert adapter.parse_test_output("", "") == []


class TestMavenSurefireOutput:
    def test_failed_tests_section(self, adapter):
        out = (
            "Tests run: 12, Failures: 1, Errors: 0, Skipped: 0\n"
            "\n"
            "Results :\n"
            "\n"
            "Failed tests: \n"
            "  FooTest.testBar:42 expected: <1> but was: <2>\n"
            "\n"
            "Tests run: 12, Failures: 1, Errors: 0, Skipped: 0\n"
        )
        results = adapter.parse_test_output(out, "")
        assert len(results) == 1
        assert results[0].name == "FooTest.testBar"
        assert results[0].passed is False

    def test_tests_in_error_section(self, adapter):
        out = (
            "Results :\n"
            "\n"
            "Tests in error: \n"
            "  BazTest.testQux » NullPointerException\n"
            "\n"
            "Tests run: 5, Failures: 0, Errors: 1, Skipped: 0\n"
        )
        results = adapter.parse_test_output(out, "")
        assert len(results) == 1
        assert results[0].name == "BazTest.testQux"
        assert results[0].passed is False

    def test_both_sections(self, adapter):
        out = (
            "Failed tests: \n"
            "  com.example.FooTest.testBar:42 expected: <1> but was: <2>\n"
            "\n"
            "Tests in error: \n"
            "  com.example.BazTest.testQux » NullPointerException\n"
            "\n"
        )
        results = adapter.parse_test_output(out, "")
        names = {r.name for r in results}
        assert names == {"com.example.FooTest.testBar", "com.example.BazTest.testQux"}
        assert all(not r.passed for r in results)

    def test_no_failures_no_entries(self, adapter):
        out = "Tests run: 5, Failures: 0, Errors: 0, Skipped: 0\nBUILD SUCCESS\n"
        assert adapter.parse_test_output(out, "") == []


class TestDetectAndDefaults:
    def test_detect_maven(self, tmp_path, adapter):
        (tmp_path / "pom.xml").write_text("<project/>")
        assert adapter.detect(tmp_path) is True
        assert adapter.default_test_command(tmp_path) == "mvn test -B"

    def test_detect_gradle_wrapper(self, tmp_path, adapter):
        (tmp_path / "build.gradle").write_text("")
        (tmp_path / "gradlew").write_text("")
        assert adapter.detect(tmp_path) is True
        assert adapter.default_test_command(tmp_path) == "./gradlew test --console=plain"

    def test_detect_gradle_kts_no_wrapper(self, tmp_path, adapter):
        (tmp_path / "build.gradle.kts").write_text("")
        assert adapter.detect(tmp_path) is True
        assert adapter.default_test_command(tmp_path) == "gradle test --console=plain"

    def test_detect_none(self, tmp_path, adapter):
        assert adapter.detect(tmp_path) is False
