"""
修正差分から、開発者が「テストで修正の正当性を保証すること」をどの程度
重視しているかを示すメトリクス（変更された関数数・新規追加テスト数など）を
算出する。

AST等の厳密な構文解析は行わず、diffのテキストに対する言語別の正規表現
ヒューリスティックで近似する（本ファイルの解析はいずれもベストエフォート）。
シグネチャ行がdiffの前後コンテキストに含まれない変更（大きな関数の本体の
奥深くだけが変更された場合等）は関数変更として数えられないため、
functions_changed は下限寄りの近似値になる。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .patch import split_diff

_DIFF_FILE_HEADER = re.compile(r'^diff --git a/(.+?) b/(.+?)$')

# 関数/メソッドのシグネチャ行に一致する言語別パターン。
# diffの行から先頭の +/-/space を除いた本文に対して先頭一致で適用する。
_FUNC_SIGNATURE_PATTERNS: dict[str, re.Pattern[str]] = {
    "rust": re.compile(
        r'^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:unsafe\s+)?'
        r'(?:extern\s+"[^"]*"\s+)?fn\s+(\w+)'
    ),
    "python": re.compile(r'^(?:async\s+)?def\s+(\w+)\s*\('),
    "java": re.compile(
        r'^(?:@\w+(?:\([^)]*\))?\s*)*'
        r'(?:(?:public|private|protected|static|final|abstract|synchronized|native|default)\s+)+'
        r'[\w<>\[\],.?]+\s+(\w+)\s*\([^;{}]*\)\s*(?:throws\s+[\w.,\s]+)?\s*\{?\s*$'
    ),
}

# 新規テスト宣言（テストファイル差分の追加行のみ）に一致する言語別パターン。
_TEST_DECL_PATTERNS: dict[str, re.Pattern[str]] = {
    "rust": re.compile(r'^#\[[\w:]*test\w*(?:\(.*\))?\]'),
    "python": re.compile(r'^(?:async\s+)?def\s+test_\w+\s*\('),
    "java": re.compile(r'^@Test\b'),
}


@dataclass
class TestEffortMetrics:
    """修正PRにおける「テストで正当性を保証する」ことへの注力度を表す指標。"""
    functions_changed: int = 0        # ソース側で差分が触れた関数/メソッド数（近似値）
    tests_added: int = 0              # テストファイル側で新規に追加されたテスト宣言数
    test_files_changed: int = 0       # 変更されたテストファイル数
    test_lines_added: int = 0         # テストファイルに追加された行数
    tests_added_per_function: float | None = None  # tests_added / functions_changed


def compute(unified_diff: str, test_patterns: list[str], language: str) -> TestEffortMetrics:
    """修正差分からテスト重視度メトリクスを算出する。"""
    if not unified_diff.strip():
        return TestEffortMetrics()

    source_diff, test_diff = split_diff(unified_diff, test_patterns)
    lang = language.lower()

    functions_changed = _count_changed_functions(source_diff, lang)
    tests_added = _count_added_tests(test_diff, lang)
    test_files_changed = _count_files(test_diff)
    test_lines_added = sum(
        1 for line in test_diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )

    return TestEffortMetrics(
        functions_changed=functions_changed,
        tests_added=tests_added,
        test_files_changed=test_files_changed,
        test_lines_added=test_lines_added,
        tests_added_per_function=(tests_added / functions_changed) if functions_changed else None,
    )


def _count_files(diff_text: str) -> int:
    return sum(1 for line in diff_text.splitlines() if line.startswith("diff --git "))


def _count_changed_functions(source_diff: str, language: str) -> int:
    pattern = _FUNC_SIGNATURE_PATTERNS.get(language)
    if pattern is None:
        return 0

    current_file: str | None = None
    seen: set[tuple[str, str]] = set()

    for raw_line in source_diff.splitlines():
        if raw_line.startswith("diff --git "):
            m = _DIFF_FILE_HEADER.match(raw_line)
            current_file = m.group(1) if m else None
            continue
        if not raw_line or raw_line[0] not in " +-":
            continue
        if raw_line.startswith(("---", "+++")):
            continue
        m = pattern.match(raw_line[1:].strip())
        if m and current_file is not None:
            seen.add((current_file, m.group(1)))

    return len(seen)


def _count_added_tests(test_diff: str, language: str) -> int:
    pattern = _TEST_DECL_PATTERNS.get(language)
    if pattern is None:
        return 0

    count = 0
    for raw_line in test_diff.splitlines():
        if not raw_line.startswith("+") or raw_line.startswith("+++"):
            continue
        if pattern.match(raw_line[1:].strip()):
            count += 1
    return count
