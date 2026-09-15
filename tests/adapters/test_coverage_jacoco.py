from __future__ import annotations

from bug_prospector.adapters.coverage.jacoco import parse_jacoco

JACOCO_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<report name="example">
  <package name="com/example">
    <class name="com/example/Foo">
      <method name="bar" line="1"/>
    </class>
    <sourcefile name="Foo.java">
      <line nr="1" mi="0" ci="4" mb="0" cb="0"/>
      <line nr="2" mi="2" ci="0" mb="0" cb="0"/>
      <line nr="3" mi="0" ci="0" mb="0" cb="0"/>
      <line nr="4" mi="0" ci="2" mb="1" cb="1"/>
      <counter type="INSTRUCTION" missed="2" covered="6"/>
      <counter type="LINE" missed="1" covered="2"/>
      <counter type="BRANCH" missed="1" covered="1"/>
    </sourcefile>
    <counter type="LINE" missed="1" covered="2"/>
  </package>
  <counter type="INSTRUCTION" missed="2" covered="6"/>
  <counter type="LINE" missed="1" covered="2"/>
  <counter type="BRANCH" missed="1" covered="1"/>
</report>
"""


def test_parse_overall_rates(tmp_path):
    report = tmp_path / "jacoco.xml"
    report.write_text(JACOCO_XML, encoding="utf-8")

    parsed = parse_jacoco(report)

    assert parsed.overall_line_rate == 2 / 3
    assert parsed.overall_branch_rate == 0.5


def test_parse_file_lines_excludes_non_executable(tmp_path):
    report = tmp_path / "jacoco.xml"
    report.write_text(JACOCO_XML, encoding="utf-8")

    parsed = parse_jacoco(report)

    fc = parsed.files["com/example/Foo.java"]
    # line 3 (mi=0, ci=0) は実行可能コードがないので除外される
    assert fc.instrumented_lines == {1, 2, 4}
    assert fc.covered_lines == {1, 4}


def test_no_line_counter_returns_none(tmp_path):
    xml = "<report><package name=\"p\"></package></report>"
    report = tmp_path / "jacoco.xml"
    report.write_text(xml, encoding="utf-8")

    parsed = parse_jacoco(report)

    assert parsed.overall_line_rate is None
    assert parsed.overall_branch_rate is None
    assert parsed.files == {}
