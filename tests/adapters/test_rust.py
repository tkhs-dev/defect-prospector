from __future__ import annotations

from pathlib import Path

import pytest

from bug_prospector.adapters.language.rust import RustAdapter

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def adapter() -> RustAdapter:
    return RustAdapter()


class TestVerboseOutput:
    def test_simple_pass(self, adapter):
        results = adapter.parse_test_output("test foo::bar ... ok\n", "")
        assert len(results) == 1
        assert results[0].name == "foo::bar"
        assert results[0].passed is True

    def test_simple_fail(self, adapter):
        results = adapter.parse_test_output("test foo::bar ... FAILED\n", "")
        assert len(results) == 1
        assert results[0].name == "foo::bar"
        assert results[0].passed is False

    def test_should_panic_pass(self, adapter):
        results = adapter.parse_test_output(
            "test foo::bar - should panic ... ok\n", ""
        )
        assert len(results) == 1
        assert results[0].name == "foo::bar - should panic"
        assert results[0].passed is True

    def test_should_panic_fail(self, adapter):
        results = adapter.parse_test_output(
            "test foo::bar - should panic ... FAILED\n", ""
        )
        assert results[0].name == "foo::bar - should panic"
        assert results[0].passed is False

    def test_doctest_pass(self, adapter):
        results = adapter.parse_test_output(
            "test src/lib.rs - (line 47) ... ok\n", ""
        )
        assert len(results) == 1
        assert results[0].name == "src/lib.rs - (line 47)"
        assert results[0].passed is True

    def test_ignored_excluded(self, adapter):
        results = adapter.parse_test_output("test foo::bar ... ignored\n", "")
        assert results == []

    def test_duration_seconds(self, adapter):
        results = adapter.parse_test_output("test foo::bar ... ok (1.23s)\n", "")
        assert results[0].duration_seconds == pytest.approx(1.23)

    def test_duration_milliseconds(self, adapter):
        results = adapter.parse_test_output("test foo::bar ... ok (450ms)\n", "")
        assert results[0].duration_seconds == pytest.approx(0.45)

    def test_summary_line_not_parsed(self, adapter):
        line = (
            "test result: ok. 5 passed; 0 failed; 0 ignored; "
            "0 measured; 0 filtered out; finished in 0.01s\n"
        )
        assert adapter.parse_test_output(line, "") == []

    def test_running_line_not_parsed(self, adapter):
        assert adapter.parse_test_output("running 5 tests\n", "") == []

    def test_stderr_also_parsed(self, adapter):
        results = adapter.parse_test_output("", "test foo::bar ... ok\n")
        assert len(results) == 1
        assert results[0].passed is True

    def test_mixed_pass_fail(self, adapter):
        out = (
            "test foo::pass1 ... ok\n"
            "test foo::fail1 ... FAILED\n"
            "test foo::pass2 ... ok\n"
            "test foo::fail2 ... FAILED\n"
        )
        results = adapter.parse_test_output(out, "")
        assert len(results) == 4
        assert sum(1 for r in results if r.passed) == 2
        assert sum(1 for r in results if not r.passed) == 2

    def test_empty_output(self, adapter):
        assert adapter.parse_test_output("", "") == []


class TestExampleOutput:
    """Testing <path> ... ok/failed 形式（cargo test の example/UI テスト）。"""

    def test_example_pass(self, adapter):
        results = adapter.parse_test_output(
            "Testing examples/foo.md:6 ... ok 8ms 467us 700ns\n", ""
        )
        assert len(results) == 1
        assert results[0].name == "example::examples/foo.md:6"
        assert results[0].passed is True

    def test_example_fail_lowercase(self, adapter):
        """'Testing' 行の失敗ステータスは小文字 'failed'。"""
        results = adapter.parse_test_output(
            "Testing tests/ui/foo.toml ... failed 9ms 723us 800ns\n", ""
        )
        assert len(results) == 1
        assert results[0].name == "example::tests/ui/foo.toml"
        assert results[0].passed is False

    def test_example_ignored_excluded(self, adapter):
        results = adapter.parse_test_output(
            "Testing examples/README.md ... ignored\n", ""
        )
        assert results == []

    def test_example_duration_not_stored(self, adapter):
        """'Testing' 行のタイミングは括弧なし形式のため duration は None。"""
        results = adapter.parse_test_output(
            "Testing examples/foo.md:6 ... ok 8ms 467us 700ns\n", ""
        )
        assert results[0].duration_seconds is None

    def test_example_without_timing(self, adapter):
        results = adapter.parse_test_output(
            "Testing examples/foo.md:6 ... ok\n", ""
        )
        assert results[0].passed is True

    def test_mixed_pass_fail_examples(self, adapter):
        out = (
            "Testing examples/a.md:1 ... ok 5ms\n"
            "Testing tests/ui/b.toml ... failed 9ms\n"
            "Testing tests/ui/c.toml ... failed 10ms\n"
            "Testing examples/d.md:2 ... ignored\n"
        )
        results = adapter.parse_test_output(out, "")
        assert len(results) == 3  # ignored は除外
        assert sum(1 for r in results if r.passed) == 1
        assert sum(1 for r in results if not r.passed) == 2


class TestQuietOutput:
    """cargo test --quiet 形式（進捗ドットのみで個別テスト行なし）。"""

    def test_failures_section(self, adapter):
        out = (
            ".F..F\n"
            "failures:\n"
            "\n"
            "---- foo::bar stdout ----\n"
            "thread 'foo::bar' panicked at ...\n"
            "\n"
            "\n"
            "failures:\n"
            "    foo::bar\n"
            "    baz::qux\n"
            "\n"
            "test result: FAILED. 3 passed; 2 failed; 0 ignored; "
            "0 measured; finished in 0.01s\n"
        )
        results = adapter.parse_test_output(out, "")
        assert len(results) == 2
        assert {r.name for r in results} == {"foo::bar", "baz::qux"}
        assert all(not r.passed for r in results)

    def test_failures_section_single(self, adapter):
        out = (
            "F\n"
            "failures:\n"
            "\n"
            "---- only_fail stdout ----\n"
            "some output\n"
            "\n"
            "\n"
            "failures:\n"
            "    only_fail\n"
            "\n"
            "test result: FAILED. 0 passed; 1 failed; finished in 0.01s\n"
        )
        results = adapter.parse_test_output(out, "")
        assert len(results) == 1
        assert results[0].name == "only_fail"
        assert results[0].passed is False

    def test_no_failures_section_empty(self, adapter):
        out = ".\n\ntest result: ok. 1 passed; 0 failed; finished in 0.01s\n"
        results = adapter.parse_test_output(out, "")
        assert results == []


class TestIntegration:
    def test_full_output_with_failures(self, adapter):
        """output.txt 全体を使った統合テスト（失敗ケースを含む実際の cargo test 出力）。"""
        content = (FIXTURES / "rust_cargo_test_failed.txt").read_text(encoding="utf-8")
        results = adapter.parse_test_output(content, "")

        passed = [r for r in results if r.passed]
        failed = [r for r in results if not r.passed]
        names = {r.name for r in results}

        # 多数の通過と複数の失敗が記録される
        assert len(passed) > 800
        assert len(failed) >= 6  # ui_tests + 5 UI サブテスト

        # ui_tests は FAILED として記録される
        assert "ui_tests" in names
        assert not next(r for r in results if r.name == "ui_tests").passed

        # Testing ... failed のサブテストが FAILED として記録される
        failed_examples = [r for r in failed if r.name.startswith("example::")]
        assert len(failed_examples) >= 5

        # "should panic" テストが正しく取得される
        panic_tests = [r for r in results if "should panic" in r.name]
        assert len(panic_tests) > 0
        assert all(r.passed for r in panic_tests)

        # doctest が記録される
        doctest = next((r for r in results if "line 47" in r.name), None)
        assert doctest is None  # このフィクスチャには doctest がない