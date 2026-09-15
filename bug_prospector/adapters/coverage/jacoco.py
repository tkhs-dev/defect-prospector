"""JaCoCo XML形式のカバレッジレポートを解析する。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .base import FileCoverage, ParsedCoverage


def parse_jacoco(report_path: Path) -> ParsedCoverage:
    root = ET.parse(report_path).getroot()

    files: dict[str, FileCoverage] = {}
    for package in root.findall("package"):
        pkg_name = package.get("name", "")
        for sourcefile in package.findall("sourcefile"):
            filename = sourcefile.get("name", "")
            normalized = f"{pkg_name}/{filename}" if pkg_name else filename
            fc = files.setdefault(normalized, FileCoverage(path=normalized))

            for line in sourcefile.findall("line"):
                nr = line.get("nr")
                if nr is None:
                    continue
                missed = int(line.get("mi", 0))
                covered = int(line.get("ci", 0))
                if missed + covered == 0:
                    continue  # 実行可能コードのない行（空行・コメント等）
                n = int(nr)
                fc.instrumented_lines.add(n)
                if covered > 0:
                    fc.covered_lines.add(n)

    return ParsedCoverage(
        files=files,
        overall_line_rate=_counter_rate(root, "LINE"),
        overall_branch_rate=_counter_rate(root, "BRANCH"),
    )


def _counter_rate(root: ET.Element, counter_type: str) -> float | None:
    """<report> 直下の <counter type="..."> からプロジェクト全体の割合を計算する。"""
    for counter in root.findall("counter"):
        if counter.get("type") != counter_type:
            continue
        missed = int(counter.get("missed", 0))
        covered = int(counter.get("covered", 0))
        total = missed + covered
        return covered / total if total else None
    return None
