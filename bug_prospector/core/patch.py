"""
差分のテスト/ソース分割およびリバースパッチ適用ユーティリティ。
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

_DIFF_HEADER = re.compile(r'^diff --git a/(.+?) b/(.+?)$')


def is_test_file(file_path: str, patterns: list[str]) -> bool:
    """ファイルパスがテストファイルパターンにマッチするか判定する。"""
    # パス区切りを統一
    normalized = file_path.replace("\\", "/")
    path = Path(normalized)

    for pattern in patterns:
        # フルパスマッチ（ワイルドカード付き）
        if fnmatch.fnmatch(normalized, pattern):
            return True
        # ファイル名のみのマッチ
        if fnmatch.fnmatch(path.name, pattern):
            return True
        # ディレクトリ前方一致（"tests/**" → "tests" ディレクトリ以下）
        prefix = pattern.rstrip("/*")
        if any(part == prefix for part in path.parts):
            return True

    return False


def split_diff(unified_diff: str, test_patterns: list[str]) -> tuple[str, str]:
    """
    unified diff をソースファイル差分とテストファイル差分に分割する。

    Returns:
        (source_diff, test_diff)
    """
    if not unified_diff.strip():
        return "", ""

    # ファイルごとのセクションに分割
    sections: list[tuple[bool, list[str]]] = []
    current_lines: list[str] = []
    current_is_test = False
    in_section = False

    for line in unified_diff.splitlines(keepends=True):
        m = _DIFF_HEADER.match(line.rstrip("\n"))
        if m:
            if in_section:
                sections.append((current_is_test, current_lines))
                current_lines = []
            in_section = True
            current_is_test = is_test_file(m.group(1), test_patterns)
            current_lines.append(line)
        else:
            current_lines.append(line)

    if in_section and current_lines:
        sections.append((current_is_test, current_lines))

    source_diff = "".join(
        "".join(lines) for is_test, lines in sections if not is_test
    )
    test_diff = "".join(
        "".join(lines) for is_test, lines in sections if is_test
    )
    return source_diff, test_diff


def apply_reverse_patch(
    repo_path: Path,
    diff: str,
    save_path: Path | None = None,
) -> tuple[bool, str]:
    """
    diff の逆パッチをワーキングツリーに適用する。

    save_path が指定された場合はパッチファイルをそこに保存して削除しない。

    Returns:
        (success, error_message)
    """
    if not diff.strip():
        return True, ""

    if save_path is not None:
        save_path.parent.mkdir(parents=True, exist_ok=True)
        patch_path = save_path
        cleanup = False
    else:
        patch_path = Path(tempfile.gettempdir()) / f"bp-patch-{uuid.uuid4().hex[:8]}.patch"
        cleanup = True

    try:
        content = diff if diff.endswith("\n") else diff + "\n"
        patch_path.write_text(content, encoding="utf-8", newline="\n")
        result = subprocess.run(
            ["git", "apply", "--reverse", "--whitespace=fix", str(patch_path)],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        print(result.stdout)
        print(result.stderr)
        return result.returncode == 0, result.stderr
    finally:
        if cleanup:
            patch_path.unlink(missing_ok=True)
