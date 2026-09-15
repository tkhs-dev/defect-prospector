from __future__ import annotations

from bug_prospector.core.test_effort import compute

RUST_PATTERNS = ["tests/**"]
JAVA_PATTERNS = ["src/test/**", "*Test.java"]
PYTHON_PATTERNS = ["tests/**", "test_*.py"]


class TestRust:
    def test_function_changed_detected(self):
        diff = (
            "diff --git a/src/lib.rs b/src/lib.rs\n"
            "--- a/src/lib.rs\n"
            "+++ b/src/lib.rs\n"
            "@@ -1,3 +1,3 @@\n"
            " fn bar(x: i32) -> i32 {\n"
            "-    x + 1\n"
            "+    x + 2\n"
            " }\n"
        )
        metrics = compute(diff, RUST_PATTERNS, "rust")
        assert metrics.functions_changed == 1
        assert metrics.tests_added == 0

    def test_new_test_counted(self):
        diff = (
            "diff --git a/tests/foo.rs b/tests/foo.rs\n"
            "--- a/tests/foo.rs\n"
            "+++ b/tests/foo.rs\n"
            "@@ -1,0 +1,4 @@\n"
            "+#[test]\n"
            "+fn it_works() {\n"
            "+    assert_eq!(1, 1);\n"
            "+}\n"
        )
        metrics = compute(diff, RUST_PATTERNS, "rust")
        assert metrics.tests_added == 1
        assert metrics.test_files_changed == 1
        assert metrics.test_lines_added == 4

    def test_ratio_none_when_no_functions_changed(self):
        metrics = compute("", RUST_PATTERNS, "rust")
        assert metrics.tests_added_per_function is None

    def test_ratio_computed(self):
        diff = (
            "diff --git a/src/lib.rs b/src/lib.rs\n"
            "--- a/src/lib.rs\n"
            "+++ b/src/lib.rs\n"
            "@@ -1,3 +1,3 @@\n"
            " fn bar(x: i32) -> i32 {\n"
            "-    x + 1\n"
            "+    x + 2\n"
            " }\n"
            "diff --git a/tests/foo.rs b/tests/foo.rs\n"
            "--- a/tests/foo.rs\n"
            "+++ b/tests/foo.rs\n"
            "@@ -1,0 +1,2 @@\n"
            "+#[test]\n"
            "+fn it_works() {}\n"
        )
        metrics = compute(diff, RUST_PATTERNS, "rust")
        assert metrics.functions_changed == 1
        assert metrics.tests_added == 1
        assert metrics.tests_added_per_function == 1.0


class TestPython:
    def test_function_and_new_test(self):
        diff = (
            "diff --git a/pkg/foo.py b/pkg/foo.py\n"
            "--- a/pkg/foo.py\n"
            "+++ b/pkg/foo.py\n"
            "@@ -1,3 +1,3 @@\n"
            " def bar(x):\n"
            "-    return x + 1\n"
            "+    return x + 2\n"
            "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -1,0 +1,2 @@\n"
            "+def test_bar():\n"
            "+    assert bar(1) == 2\n"
        )
        metrics = compute(diff, PYTHON_PATTERNS, "python")
        assert metrics.functions_changed == 1
        assert metrics.tests_added == 1

    def test_async_def_test(self):
        diff = (
            "diff --git a/tests/test_foo.py b/tests/test_foo.py\n"
            "--- a/tests/test_foo.py\n"
            "+++ b/tests/test_foo.py\n"
            "@@ -1,0 +1,1 @@\n"
            "+async def test_bar():\n"
        )
        metrics = compute(diff, PYTHON_PATTERNS, "python")
        assert metrics.tests_added == 1


class TestJava:
    def test_method_changed_and_test_added(self):
        diff = (
            "diff --git a/src/main/java/Foo.java b/src/main/java/Foo.java\n"
            "--- a/src/main/java/Foo.java\n"
            "+++ b/src/main/java/Foo.java\n"
            "@@ -1,3 +1,3 @@\n"
            " public int bar(int x) {\n"
            "-    return x + 1;\n"
            "+    return x + 2;\n"
            "diff --git a/src/test/java/FooTest.java b/src/test/java/FooTest.java\n"
            "--- a/src/test/java/FooTest.java\n"
            "+++ b/src/test/java/FooTest.java\n"
            "@@ -1,0 +1,3 @@\n"
            "+@Test\n"
            "+public void testBar() {\n"
            "+}\n"
        )
        metrics = compute(diff, JAVA_PATTERNS, "java")
        assert metrics.functions_changed == 1
        assert metrics.tests_added == 1

    def test_class_declaration_not_counted_as_function(self):
        diff = (
            "diff --git a/src/main/java/Foo.java b/src/main/java/Foo.java\n"
            "--- a/src/main/java/Foo.java\n"
            "+++ b/src/main/java/Foo.java\n"
            "@@ -1,1 +1,1 @@\n"
            "-public class Foo {\n"
            "+public class Foo implements Bar {\n"
        )
        metrics = compute(diff, JAVA_PATTERNS, "java")
        assert metrics.functions_changed == 0


class TestUnknownLanguage:
    def test_unsupported_language_returns_zero_functions_and_tests(self):
        diff = (
            "diff --git a/src/foo.go b/src/foo.go\n"
            "--- a/src/foo.go\n"
            "+++ b/src/foo.go\n"
            "@@ -1,1 +1,1 @@\n"
            "-func Bar() {}\n"
            "+func Bar() { return }\n"
        )
        metrics = compute(diff, [], "go")
        assert metrics.functions_changed == 0
        assert metrics.tests_added == 0
        assert metrics.tests_added_per_function is None


class TestEmptyDiff:
    def test_empty_diff_returns_zeroed_metrics(self):
        metrics = compute("", ["tests/**"], "rust")
        assert metrics.functions_changed == 0
        assert metrics.tests_added == 0
        assert metrics.test_files_changed == 0
        assert metrics.test_lines_added == 0
        assert metrics.tests_added_per_function is None
