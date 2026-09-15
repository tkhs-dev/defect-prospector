"""Cobertura XML形式のカバレッジレポートを解析する（cargo-llvm-cov --cobertura の出力）。"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from .base import FileCoverage, ParsedCoverage


def parse_cobertura(report_path: Path) -> ParsedCoverage:
    root = ET.parse(report_path).getroot()

    files: dict[str, FileCoverage] = {}
    for cls in root.iter("class"):
        filename = cls.get("filename")
        if not filename:
            continue
        normalized = filename.replace("\\", "/").lstrip("/")
        fc = files.setdefault(normalized, FileCoverage(path=normalized))

        lines_el = cls.find("lines")
        if lines_el is None:
            continue
        for line in lines_el.findall("line"):
            number = line.get("number")
            if number is None:
                continue
            n = int(number)
            fc.instrumented_lines.add(n)
            if int(line.get("hits", 0)) > 0:
                fc.covered_lines.add(n)

    return ParsedCoverage(
        files=files,
        overall_line_rate=_float(root.get("line-rate")),
        overall_branch_rate=_float(root.get("branch-rate")),
    )


def _float(value: str | None) -> float | None:
    return float(value) if value is not None else None
