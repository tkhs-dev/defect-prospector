"""
PR起点・直接コミット収集の補助関数（doc/pr-collection.md）。

- マージ方式（マージコミット / squash / rebase）に応じた buggy コミットの特定
- LLM判定の前に行う安価な事前フィルタ（タイトルのキーワード、bot、ソース変更の有無・規模）
"""
from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .patch import split_diff

MERGE_COMMIT = "merge"
SQUASH = "squash"
REBASE = "rebase"

_CLOSING_REF_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[^\n#]{0,80}?(?:#|/issues/)(\d+)\b", re.IGNORECASE,
)


@dataclass
class FixRange:
    buggy_sha: str
    fix_sha: str
    merge_method: str


def resolve_pr_fix_range(repo_path: Path, merge_sha: str, pr_commit_subjects: list[str]) -> FixRange:
    """
    PRの merge_commit_sha から buggy コミットを決める。

    - 親が2つ: マージコミット。buggy = first parent
    - 親が1つでPRのコミットが k(>1) 個、かつ first-parent を k 個遡った件名がPRのコミット件名と一致:
      rebase。buggy = merge_sha~k（PRの全コミットを含める）
    - それ以外: squash（または1コミットのPR）。buggy = parent
    """
    parents = _git(repo_path, "rev-list", "--parents", "-n", "1", merge_sha).split()[1:]
    if len(parents) >= 2:
        return FixRange(parents[0], merge_sha, MERGE_COMMIT)
    k = len(pr_commit_subjects)
    if k > 1:
        chain = _git(repo_path, "log", "--first-parent", f"-{k}", "--format=%s", merge_sha).splitlines()
        if sorted(s.strip() for s in chain) == sorted(s.strip() for s in pr_commit_subjects):
            buggy = _git(repo_path, "rev-parse", f"{merge_sha}~{k}").strip()
            return FixRange(buggy, merge_sha, REBASE)
    return FixRange(parents[0] if parents else merge_sha, merge_sha, SQUASH)


def matches_keywords(title: str, keywords: list[str] | None) -> bool:
    """keywords が None なら常に True。単語の前方一致（"fix" は "fixes"/"fixed" にもマッチ）、大文字小文字無視。"""
    if keywords is None:
        return True
    return any(re.search(rf"\b{re.escape(k)}", title, re.IGNORECASE) for k in keywords)


def is_bot(login_or_name: str, user_type: str = "", email: str = "") -> bool:
    return user_type == "Bot" or "[bot]" in login_or_name or "[bot]" in email


def source_change_rejection(
    diff: str, test_patterns: list[str], ignore_paths: list[str], max_source_lines: int | None,
) -> str | None:
    """
    ソース変更の観点で候補から外す理由を返す（外さないなら None）。
    - テスト以外の変更が無い（逆パッチ検証ができない）
    - テスト以外の変更が ignore_paths（ドキュメント・CI・設定等）だけ
    - ソース差分の行数が max_source_lines を超える（機能追加・リファクタリングの可能性が高い）
    """
    source_diff, _ = split_diff(diff, test_patterns)
    if not source_diff.strip():
        return "no source changes (tests only)"
    files = changed_files(source_diff)
    code_files = [f for f in files if not any(fnmatch.fnmatch(f, p) for p in ignore_paths)]
    if not code_files:
        return "only non-code files changed: " + ", ".join(files[:5])
    if max_source_lines is not None:
        n = sum(
            1 for line in source_diff.splitlines()
            if (line.startswith("+") and not line.startswith("+++"))
            or (line.startswith("-") and not line.startswith("---"))
        )
        if n > max_source_lines:
            return f"source diff too large ({n} lines > {max_source_lines})"
    return None


def changed_files(diff: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", diff, re.MULTILINE)


def closing_issue_refs(text: str) -> list[int]:
    """PR本文・タイトルから "Fixes #N" 等で修正を明言している Issue 番号を取り出す。"""
    return [int(n) for n in _CLOSING_REF_RE.findall(text or "")]


def first_parent_commits(repo_path: Path, branch: str, since: str | None, until: str | None) -> list[dict]:
    """デフォルトブランチの first-parent 上の非マージコミットを新しい順に返す（直接コミットの候補）。"""
    args = ["log", branch, "--first-parent", "--no-merges", "--format=%H%x1f%an%x1f%ae%x1f%cI%x1f%s%x1f%b%x1e"]
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    commits = []
    for entry in _git(repo_path, *args).split("\x1e"):
        parts = entry.strip("\n").split("\x1f")
        if len(parts) < 6:
            continue
        sha, name, email, date, subject, body = parts
        commits.append({
            "sha": sha.strip(), "author_name": name, "author_email": email,
            "date": date, "subject": subject, "body": body.strip(),
        })
    return commits


def _git(repo_path: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", *args], cwd=repo_path, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:500]}")
    return r.stdout
