from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import click
from dotenv import load_dotenv

load_dotenv()


@click.group()
@click.option(
    "--dataset-dir",
    default="./dataset",
    show_default=True,
    envvar="BP_DATASET_DIR",
    help="Dataset storage directory",
)
@click.option("--verbose", "-v", is_flag=True)
@click.pass_context
def cli(ctx: click.Context, dataset_dir: str, verbose: bool) -> None:
    ctx.ensure_object(dict)
    ctx.obj["dataset_dir"] = Path(dataset_dir).resolve()
    ctx.obj["verbose"] = verbose
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
    )


@cli.command()
@click.option(
    "--project", "-p",
    "project_file",
    default=None,
    type=click.Path(exists=True),
    help="Per-project YAML config file (recommended)",
)
@click.option(
    "--tracker-url",
    default=None,
    help="GitHub repository URL (used without --project)",
)
@click.option(
    "--repo-url",
    default=None,
    help="Git clone URL (defaults to tracker-url)",
)
@click.option(
    "--token",
    envvar="GITHUB_TOKEN",
    default=None,
    show_default="env:GITHUB_TOKEN",
    help="GitHub personal access token",
)
@click.option(
    "--labels",
    default=None,
    help="Override: comma-separated issue labels (e.g. bug,regression)",
)
@click.option("--limit", type=int, default=None, help="Override: max issues to process")
@click.option(
    "--since",
    default=None,
    metavar="DATE",
    help="Override: collect issues created on/after DATE (ISO 8601)",
)
@click.option("--dry-run", is_flag=True, help="Preview only. Nothing is saved.")
@click.pass_context
def collect(
    ctx: click.Context,
    project_file: str | None,
    tracker_url: str | None,
    repo_url: str | None,
    token: str | None,
    labels: str | None,
    limit: int | None,
    since: str | None,
    dry_run: bool,
) -> None:
    """Collect bug records.

    Use --project to load a per-project YAML config (recommended).
    CLI options override values in the config file.

    \b
    Examples:
      bp collect --project flask.yaml
      bp collect --project flask.yaml --limit 10 --dry-run
      bp collect --tracker-url https://github.com/pallets/flask --token $TOKEN
    """
    from bug_prospector.config import ProjectConfig, SearchConfig, TrackerAuth
    from bug_prospector.core.collector import Collector
    from bug_prospector.core.storage import Storage

    dataset_dir: Path = ctx.obj["dataset_dir"]

    # --- Config の組み立て ---
    if project_file:
        project = ProjectConfig.from_file(Path(project_file))
        # CLI オプションでの上書き
        if token:
            project.tracker_auth = TrackerAuth(token=token)
        if tracker_url:
            project.tracker_url = tracker_url
        if repo_url:
            project.repo_url = repo_url
        if labels:
            project.search.labels = [l.strip() for l in labels.split(",") if l.strip()]
        if since:
            project.search.since = since
    else:
        # --project なし → CLI オプションから最小限の ProjectConfig を生成
        if not tracker_url:
            raise click.UsageError("Either --project or --tracker-url is required.")
        effective_token = token or os.environ.get("GITHUB_TOKEN", "")
        from bug_prospector.config import (
            LanguageConfig, ValidationConfig, ProjectConfig, SearchConfig, TrackerAuth
        )
        project = ProjectConfig(
            project_id="",
            repo_url=repo_url or tracker_url,
            tracker_url=tracker_url,
            tracker_auth=TrackerAuth(token=effective_token),
            search=SearchConfig(
                labels=[l.strip() for l in (labels or "bug").split(",")],
                require_pr=True,
                since=since,
                limit=limit,
            ),
        )

    if dry_run:
        click.echo("[dry-run] No data will be saved.")
    click.echo(f"Collecting bugs from {project.tracker_url} ...")
    if project.validation.enabled:
        click.echo(f"  Validation enabled  (strategy={project.validation.strategy})")

    from bug_prospector.core.collector import CollectionInterrupted

    storage = Storage(dataset_dir)
    collector = Collector(storage=storage, dataset_dir=dataset_dir)

    try:
        new, skipped, failed = collector.run_project(
            project=project,
            limit_override=limit,
            dry_run=dry_run,
        )
        click.echo(f"\nCollected: {new} new  |  {skipped} skipped  |  {failed} failed")
    except CollectionInterrupted as e:
        click.echo(
            f"\n[interrupted] Collected so far: {e.new} new  |  {e.skipped} skipped  |  {e.failed} failed"
        )
        raise SystemExit(1)


@cli.command()
@click.option("--tracker", default=None, help="Filter by tracker URL (prefix match)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def stats(ctx: click.Context, tracker: str | None, as_json: bool) -> None:
    """Show statistics of the collected dataset."""
    import json
    import statistics

    from bug_prospector.core.storage import Storage
    from bug_prospector.core.models.record import VALID, INVALID, PATCH_FAILED, SETUP_FAILED, TIMEOUT, ERROR, SKIPPED

    dataset_dir: Path = ctx.obj["dataset_dir"]
    storage = Storage(dataset_dir)
    records = storage.list_all()

    if tracker:
        records = [r for r in records if r.source.tracker_url.startswith(tracker)]

    total = len(records)
    if total == 0:
        click.echo("No records found.")
        return

    # --- language breakdown ---
    lang_counts: dict[str, int] = {}
    for r in records:
        lang = r.reproduction.environment.language or "unknown"
        lang_counts[lang] = lang_counts.get(lang, 0) + 1

    # --- tracker breakdown ---
    tracker_counts: dict[str, int] = {}
    for r in records:
        key = r.source.tracker_url
        tracker_counts[key] = tracker_counts.get(key, 0) + 1

    # --- validation status ---
    val_counts: dict[str, int] = {}
    for r in records:
        if r.validation:
            s = r.validation.status
        else:
            s = SKIPPED
        val_counts[s] = val_counts.get(s, 0) + 1

    # --- fix size stats ---
    lines_added = [r.fix.lines_added for r in records]
    lines_deleted = [r.fix.lines_deleted for r in records]
    changed_files = [len(r.fix.changed_files) for r in records]

    def _stats(vals: list[int]) -> dict:
        if not vals:
            return {}
        return {
            "min": min(vals),
            "max": max(vals),
            "mean": round(sum(vals) / len(vals), 1),
            "median": round(statistics.median(vals), 1),
        }

    # --- date range ---
    dates = sorted(r.metadata.collected_at for r in records)
    date_range = (dates[0].date().isoformat(), dates[-1].date().isoformat())

    # --- issue age (days from created_at to closed_at) ---
    ages = []
    for r in records:
        if r.source.created_at and r.source.closed_at:
            ages.append((r.source.closed_at - r.source.created_at).days)

    # --- test case stats ---
    # validation がある全レコードを対象に集計する

    # テスト実行自体の失敗内訳
    exec_fail_counts: dict[str, int] = {}
    for s in (PATCH_FAILED, SETUP_FAILED, TIMEOUT, ERROR):
        n = val_counts.get(s, 0)
        if n:
            exec_fail_counts[s] = n

    # ケース単位のデータがあるレコードで集計
    # fixed       : buggy FAIL → fix PASS（バグ修正で直ったテスト）
    # preexisting : buggy FAIL → fix FAIL（修正に無関係な既存失敗）
    # regression  : buggy PASS → fix FAIL（修正で新たに壊れたテスト）
    n_with_cases = 0
    n_fallback = 0  # test_cases なし・全体結果のみ
    fixed_counts: list[int] = []
    preexisting_counts: list[int] = []
    regression_counts: list[int] = []

    for r in records:
        v = r.validation
        if v is None or v.fix_result is None or v.buggy_result is None:
            continue
        fr, br = v.fix_result, v.buggy_result
        if fr.test_cases and br.test_cases:
            n_with_cases += 1
            fix_passed  = {tc.name for tc in fr.test_cases if     tc.passed}
            fix_failed  = {tc.name for tc in fr.test_cases if not tc.passed}
            buggy_passed = {tc.name for tc in br.test_cases if     tc.passed}
            buggy_failed = {tc.name for tc in br.test_cases if not tc.passed}
            fixed_counts.append(len(buggy_failed & fix_passed))
            preexisting_counts.append(len(buggy_failed & fix_failed))
            regression_counts.append(len(buggy_passed & fix_failed))
        else:
            n_fallback += 1

    def _dist(vals: list[int]) -> dict[str, int]:
        d: dict[str, int] = {}
        for n in vals:
            if n == 0:
                k = "0"
            elif n == 1:
                k = "1"
            elif n == 2:
                k = "2"
            elif n <= 5:
                k = "3-5"
            elif n <= 10:
                k = "6-10"
            else:
                k = "11+"
            d[k] = d.get(k, 0) + 1
        return d

    test_stats = {
        "with_case_data": n_with_cases,
        "fallback_overall_only": n_fallback,
        "exec_failures": exec_fail_counts,
        "fixed_per_bug":       _stats(fixed_counts),
        "preexisting_per_bug": _stats(preexisting_counts),
        "regression_per_bug":  _stats(regression_counts),
        "fixed_distribution":       _dist(fixed_counts),
        "preexisting_distribution": _dist(preexisting_counts),
    }

    result = {
        "total": total,
        "date_range": {"from": date_range[0], "to": date_range[1]},
        "by_language": lang_counts,
        "by_tracker": tracker_counts,
        "validation": val_counts,
        "fix_lines_added": _stats(lines_added),
        "fix_lines_deleted": _stats(lines_deleted),
        "changed_files_per_bug": _stats(changed_files),
        "issue_age_days": _stats(ages) if ages else {},
        "test_cases": test_stats,
    }

    if as_json:
        click.echo(json.dumps(result, ensure_ascii=False, indent=2))
        return

    def _bar(counts: dict[str, int], total: int, width: int = 20) -> None:
        for key, n in sorted(counts.items(), key=lambda x: -x[1]):
            pct = n / total * 100
            bar = "#" * round(pct / 100 * width)
            click.echo(f"  {key:<35}  {n:>5}  ({pct:5.1f}%)  {bar}")

    def _fmt_stats(s: dict) -> str:
        if not s:
            return "n/a"
        return f"min={s['min']}  max={s['max']}  mean={s['mean']}  median={s['median']}"

    click.echo(f"\n{'='*60}")
    click.echo(f"  Dataset statistics  (total: {total})")
    click.echo(f"  Collected: {date_range[0]} to {date_range[1]}")
    click.echo(f"{'='*60}")

    click.echo("\n[Language]")
    _bar(lang_counts, total)

    click.echo("\n[Tracker]")
    _bar(tracker_counts, total)

    click.echo("\n[Validation status]")
    _bar(val_counts, total)

    click.echo("\n[Fix size]")
    click.echo(f"  lines added   : {_fmt_stats(_stats(lines_added))}")
    click.echo(f"  lines deleted : {_fmt_stats(_stats(lines_deleted))}")
    click.echo(f"  changed files : {_fmt_stats(_stats(changed_files))}")

    if ages:
        click.echo("\n[Issue age (days to close)]")
        click.echo(f"  {_fmt_stats(_stats(ages))}")

    n_with_cases = test_stats["with_case_data"]
    n_fallback   = test_stats["fallback_overall_only"]
    exec_fails   = test_stats["exec_failures"]

    click.echo(f"\n[Test cases]")
    click.echo(f"  with per-case data   : {n_with_cases}")
    click.echo(f"  fallback (overall)   : {n_fallback}")
    if exec_fails:
        click.echo("  execution failures:")
        for s, n in sorted(exec_fails.items(), key=lambda x: -x[1]):
            click.echo(f"    {s:<20} {n}")

    def _dist_bar(dist: dict[str, int], base: int, label: str) -> None:
        if not dist:
            return
        click.echo(f"  {label}:")
        for bucket in ["0", "1", "2", "3-5", "6-10", "11+"]:
            n = dist.get(bucket, 0)
            if n == 0:
                continue
            pct = n / base * 100
            bar = "#" * round(pct / 100 * 20)
            click.echo(f"    {bucket:>5}  {n:>5}  ({pct:5.1f}%)  {bar}")

    if n_with_cases:
        click.echo(f"\n  [buggy FAIL → fix PASS  (bug-fixing tests)]")
        click.echo(f"    {_fmt_stats(test_stats['fixed_per_bug'])}")
        _dist_bar(test_stats["fixed_distribution"], n_with_cases, "distribution")

        click.echo(f"\n  [FAIL in both states  (pre-existing failures)]")
        click.echo(f"    {_fmt_stats(test_stats['preexisting_per_bug'])}")
        _dist_bar(test_stats["preexisting_distribution"], n_with_cases, "distribution")

        if any(v > 0 for v in test_stats["regression_per_bug"].values()):
            click.echo(f"\n  [buggy PASS → fix FAIL  (regressions)]")
            click.echo(f"    {_fmt_stats(test_stats['regression_per_bug'])}")

    click.echo("")


if __name__ == "__main__":
    cli()
