from __future__ import annotations

from pathlib import Path

from bug_prospector.adapters.coverage import get_coverage_adapter
from bug_prospector.adapters.coverage.python import CoveragePyAdapter

COBERTURA_XML = """<?xml version="1.0" ?>
<coverage line-rate="0.5" branch-rate="0.0" version="0.1">
  <packages>
    <package name="pkg">
      <classes>
        <class name="foo" filename="pkg/foo.py" line-rate="0.5" branch-rate="0.0">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="0"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""


def test_registered_for_python():
    adapter = get_coverage_adapter("python")
    assert isinstance(adapter, CoveragePyAdapter)


def test_default_report_path(tmp_path):
    adapter = CoveragePyAdapter()
    assert adapter.default_report_path(tmp_path) == tmp_path / "coverage.xml"


def test_parse_report_reuses_cobertura_parser(tmp_path):
    report = tmp_path / "coverage.xml"
    report.write_text(COBERTURA_XML, encoding="utf-8")

    parsed = CoveragePyAdapter().parse_report(report)

    assert parsed.overall_line_rate == 0.5
    fc = parsed.files["pkg/foo.py"]
    assert fc.instrumented_lines == {1, 2}
    assert fc.covered_lines == {1}
