from __future__ import annotations

from bug_prospector.adapters.coverage.cobertura import parse_cobertura

COBERTURA_XML = """<?xml version="1.0" ?>
<coverage line-rate="0.75" branch-rate="0.5" version="0.1">
  <packages>
    <package name="crate">
      <classes>
        <class name="foo" filename="src/foo.rs" line-rate="0.75" branch-rate="0.5">
          <lines>
            <line number="1" hits="3" branch="false"/>
            <line number="2" hits="0" branch="false"/>
            <line number="3" hits="1" branch="false"/>
            <line number="4" hits="0" branch="false"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""


def test_parse_overall_rates(tmp_path):
    report = tmp_path / "cobertura.xml"
    report.write_text(COBERTURA_XML, encoding="utf-8")

    parsed = parse_cobertura(report)

    assert parsed.overall_line_rate == 0.75
    assert parsed.overall_branch_rate == 0.5


def test_parse_file_lines(tmp_path):
    report = tmp_path / "cobertura.xml"
    report.write_text(COBERTURA_XML, encoding="utf-8")

    parsed = parse_cobertura(report)

    fc = parsed.files["src/foo.rs"]
    assert fc.instrumented_lines == {1, 2, 3, 4}
    assert fc.covered_lines == {1, 3}


def test_backslash_paths_normalized(tmp_path):
    xml = COBERTURA_XML.replace('filename="src/foo.rs"', 'filename="src\\foo.rs"')
    report = tmp_path / "cobertura.xml"
    report.write_text(xml, encoding="utf-8")

    parsed = parse_cobertura(report)

    assert "src/foo.rs" in parsed.files
