"""
修正の機械的な確認（doc/fix-checks.md）。

収集した「修正」が本当に正しい修正だったかは保証できないが、事後の履歴から疑わしいものに印を付ける:

  revert         後続のコミットでリバートされていないか
  follow_up_fix  修正で追加したソース行が、短期間（既定30日）のうちに書き換えられていないか
                 （不完全な修正を直す「修正の修正」の疑い）
  release        修正コミットを含むタグ（リリース）があるか
  ci             CIが失敗していないか（PRならマージ前のPRのhead、それ以外は修正コミット）

各確認は FixCheckConfig で off / record / exclude を選ぶ。exclude で passed=False になったレコードは
呼び出し側（Collector）が validation.status=fix_rejected としてテスト検証の対象から外す。
"""
from __future__ import annotations

import logging
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from ..config import CHECK_EXCLUDE, CHECK_OFF, FIX_CHECK_NAMES, FixCheckConfig
from .models.record import FixCheckResult

logger = logging.getLogger(__name__)

# sha → {"state": "success"|"failure"|"pending"|"none", ...}。GitHubAdapter.get_ci_status を渡す
CiLookup = Callable[[str], dict]

_REVERTS_RE = re.compile(r"reverts commit ([0-9a-f]{7,40})", re.IGNORECASE)
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


class FixChecker:
    """1リポジトリ分の確認を行う。branch はデフォルトブランチの ref（例: "origin/master"）。"""

    def __init__(self, repo_path: Path, branch: str, ci_lookup: CiLookup | None = None) -> None:
        self._repo = repo_path
        self._branch = branch
        self._ci_lookup = ci_lookup

    def run(
        self,
        config: FixCheckConfig,
        fix_sha: str,
        buggy_sha: str,
        source_diff: str,
        ci_shas: list[str] | None = None,
    ) -> dict[str, FixCheckResult]:
        """config で off 以外の確認を実行する。個々の確認が例外を出したら passed=None として記録する。"""
        runners = {
            "revert": lambda: self.check_revert(fix_sha, buggy_sha),
            "follow_up_fix": lambda: self.check_follow_up_fix(fix_sha, source_diff, config.follow_up_window_days),
            "release": lambda: self.check_release(fix_sha),
            "ci": lambda: self.check_ci(ci_shas or [fix_sha]),
        }
        results: dict[str, FixCheckResult] = {}
        for name in FIX_CHECK_NAMES:
            if config.mode(name) == CHECK_OFF:
                continue
            try:
                results[name] = runners[name]()
            except Exception as e:
                logger.debug(f"fix check {name} failed: {e}")
                results[name] = FixCheckResult(passed=None, detail=f"check failed: {e}")
        return results

    # ------------------------------------------------------------------
    # 個々の確認
    # ------------------------------------------------------------------

    def check_revert(self, fix_sha: str, buggy_sha: str) -> FixCheckResult:
        # 修正を構成する全コミット（マージコミットやrebaseされた複数コミットのPRも含む）
        fix_commits = set(self._git("rev-list", f"{buggy_sha}..{fix_sha}").split()) | {fix_sha}
        fix_subject = self._git("log", "-1", "--format=%s", fix_sha).strip()
        log = self._git("log", "--format=%H%x1f%s%x1f%b%x1e", f"{fix_sha}..{self._branch}")
        for entry in log.split("\x1e"):
            parts = entry.strip().split("\x1f")
            if len(parts) < 3:
                continue
            sha, subject, body = parts
            reverted = [m for m in _REVERTS_RE.findall(body) if any(c.startswith(m) for c in fix_commits)]
            if reverted or subject == f'Revert "{fix_subject}"':
                return FixCheckResult(
                    passed=False,
                    detail=f"reverted by {sha[:12]}: {subject}",
                    data={"revert_commit": sha, "subject": subject},
                )
        return FixCheckResult(passed=True, detail="not reverted")

    def check_follow_up_fix(self, fix_sha: str, source_diff: str, window_days: int) -> FixCheckResult:
        added = added_lines(source_diff)
        if not added:
            return FixCheckResult(passed=True, detail="fix adds no source lines")

        fix_time = datetime.fromisoformat(self._git("log", "-1", "--format=%cI", fix_sha).strip())
        window_end = fix_time + timedelta(days=window_days)
        end = self._git(
            "rev-list", "-1", "--first-parent", f"--before={window_end.isoformat()}", self._branch,
        ).strip()
        if not end or end == fix_sha or not self._is_ancestor(fix_sha, end):
            return FixCheckResult(passed=True, detail=f"no later commits within {window_days} days")

        changed: dict[str, int] = {}
        changers: set[str] = set()
        for path, lines in added.items():
            # --reverse: fix 時点の各行が「最後に存在したコミット」。end 以外なら窓内で書き換え/削除された
            out = self._git("blame", "--reverse", "-p", f"{fix_sha}..{end}", "--", path)
            for last_sha, line_no in _blame_lines(out):
                if line_no in lines and last_sha != end:
                    changed[path] = changed.get(path, 0) + 1
                    changers.add(last_sha)

        total = sum(len(v) for v in added.values())
        n_changed = sum(changed.values())
        if not n_changed:
            return FixCheckResult(
                passed=True, detail=f"all {total} added line(s) unchanged for {window_days} days",
                data={"added_lines": total},
            )
        # 書き換えたコミットは「最後に存在したコミット」の子。first-parent 上の次のコミットを特定する
        follow_ups = sorted({self._next_first_parent(sha, end) for sha in changers} - {""})
        subjects = [self._git("log", "-1", "--format=%h %s", s).strip() for s in follow_ups]
        return FixCheckResult(
            passed=False,
            detail=f"{n_changed}/{total} added line(s) changed within {window_days} days by: " + "; ".join(subjects),
            data={"added_lines": total, "changed_lines": changed, "follow_up_commits": follow_ups},
        )

    def check_release(self, fix_sha: str) -> FixCheckResult:
        tags = self._git("tag", "--contains", fix_sha, "--sort=creatordate").split()
        if not tags:
            return FixCheckResult(passed=False, detail="not contained in any tag (unreleased)")
        return FixCheckResult(
            passed=True, detail=f"first released in {tags[0]}",
            data={"first_tag": tags[0], "tag_count": len(tags)},
        )

    def check_ci(self, shas: list[str]) -> FixCheckResult:
        if self._ci_lookup is None:
            return FixCheckResult(passed=None, detail="no CI lookup available")
        states = {sha: self._ci_lookup(sha) for sha in dict.fromkeys(s for s in shas if s)}
        summary = {sha: s.get("state", "none") for sha, s in states.items()}
        detail = ", ".join(f"{sha[:12]}={state}" for sha, state in summary.items())
        if any(state == "failure" for state in summary.values()):
            return FixCheckResult(passed=False, detail=f"CI failed: {detail}", data={"states": states})
        if any(state == "success" for state in summary.values()):
            return FixCheckResult(passed=True, detail=f"CI passed: {detail}", data={"states": states})
        return FixCheckResult(passed=None, detail=f"no conclusive CI result: {detail}", data={"states": states})

    # ------------------------------------------------------------------
    # git helpers
    # ------------------------------------------------------------------

    def _git(self, *args: str) -> str:
        r = subprocess.run(
            ["git", *args], cwd=self._repo, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        if r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:500]}")
        return r.stdout

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        r = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, descendant], cwd=self._repo, capture_output=True,
        )
        return r.returncode == 0

    def _next_first_parent(self, sha: str, end: str) -> str:
        """first-parent 上で sha の次のコミット（sha..end の最古）。見つからなければ空文字。"""
        out = self._git("rev-list", "--first-parent", "--reverse", f"{sha}..{end}").split()
        return out[0] if out else ""


def rejected_checks(results: dict[str, FixCheckResult], config: FixCheckConfig) -> list[str]:
    """exclude モードで passed=False になった確認の名前。空でなければレコードは fix_rejected になる。"""
    return [n for n, r in results.items() if r.passed is False and config.mode(n) == CHECK_EXCLUDE]


def added_lines(diff: str) -> dict[str, set[int]]:
    """unified diff から、ファイルごとの追加行（新しい側の行番号）を取り出す。削除ファイルは除く。"""
    result: dict[str, set[int]] = {}
    path: str | None = None
    new_line = 0
    for line in diff.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            path = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            continue
        if line.startswith("--- ") or line.startswith("diff --git"):
            continue
        m = _HUNK_RE.match(line)
        if m:
            new_line = int(m.group(1))
            continue
        if path is None:
            continue
        if line.startswith("+"):
            result.setdefault(path, set()).add(new_line)
            new_line += 1
        elif line.startswith("-"):
            continue
        elif line.startswith("\\"):
            continue
        else:
            new_line += 1
    return {p: s for p, s in result.items() if s}


def _blame_lines(porcelain: str):
    """git blame -p の出力から (コミットSHA, 対象版での行番号) を順に返す。"""
    for line in porcelain.splitlines():
        parts = line.split(" ")
        if len(parts) >= 3 and len(parts[0]) == 40 and all(c in "0123456789abcdef" for c in parts[0]):
            try:
                yield parts[0], int(parts[2])
            except ValueError:
                continue
