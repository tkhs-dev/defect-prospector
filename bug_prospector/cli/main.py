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

    def _echo_validation_counts(counts: dict[str, int]) -> None:
        if not counts:
            return
        breakdown = ", ".join(
            f"{s}={n}" for s, n in sorted(counts.items(), key=lambda x: -x[1])
        )
        click.echo(f"  validation status: {breakdown}")

    try:
        new, skipped, failed, validation_counts = collector.run_project(
            project=project,
            limit_override=limit,
            dry_run=dry_run,
        )
        click.echo(f"\nCollected: {new} new  |  {skipped} skipped  |  {failed} failed")
        _echo_validation_counts(validation_counts)
    except CollectionInterrupted as e:
        click.echo(
            f"\n[interrupted] Collected so far: {e.new} new  |  {e.skipped} skipped  |  {e.failed} failed"
        )
        _echo_validation_counts(e.validation_counts)
        raise SystemExit(1)


@cli.command()
@click.option("--tracker", default=None, help="Filter by tracker URL (prefix match)")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def stats(ctx: click.Context, tracker: str | None, as_json: bool) -> None:
    """Show statistics of the collected dataset."""
    import json
    import statistics

    from bug_prospector.adapters.language import get_language_adapter
    from bug_prospector.core.storage import Storage
    from bug_prospector.core.models.record import VALID, INVALID, PATCH_FAILED, SETUP_FAILED, TIMEOUT, ERROR, SKIPPED
    from bug_prospector.core.test_effort import compute as compute_test_effort

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

    # --- test discipline (test-driven fix-correctness effort) ---
    # 「修正の正当性をテストで保証することをどれだけ重視しているか」を示す指標:
    # 差分が触れた関数数・新規追加テスト数・その比率を言語アダプタのテストパターンを
    # 使って diff から近似する。
    _patterns_cache: dict[str, list[str]] = {}
    functions_changed: list[int] = []
    tests_added: list[int] = []
    tests_added_per_function: list[float] = []
    prs_with_new_tests = 0
    for r in records:
        lang = r.reproduction.environment.language or ""
        if lang not in _patterns_cache:
            adapter = get_language_adapter(lang)
            _patterns_cache[lang] = adapter.default_test_patterns() if adapter else []
        effort = compute_test_effort(r.fix.diff, _patterns_cache[lang], lang)
        functions_changed.append(effort.functions_changed)
        tests_added.append(effort.tests_added)
        if effort.tests_added > 0:
            prs_with_new_tests += 1
        if effort.tests_added_per_function is not None:
            tests_added_per_function.append(effort.tests_added_per_function)

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
        "test_discipline": {
            "functions_changed_per_bug": _stats(functions_changed),
            "tests_added_per_bug": _stats(tests_added),
            "prs_with_new_tests_rate": round(prs_with_new_tests / total, 3),
            "tests_added_per_function_changed": _stats(tests_added_per_function),
        },
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

    click.echo("\n[Test discipline]  (how much fixes lean on tests to prove correctness)")
    click.echo(f"  functions changed/bug : {_fmt_stats(result['test_discipline']['functions_changed_per_bug'])}")
    click.echo(f"  new tests added/bug   : {_fmt_stats(result['test_discipline']['tests_added_per_bug'])}")
    click.echo(f"  bugs adding a new test: {result['test_discipline']['prs_with_new_tests_rate']*100:5.1f}%")
    tapf = result["test_discipline"]["tests_added_per_function_changed"]
    if tapf:
        click.echo(f"  new tests / function changed: {_fmt_stats(tapf)}")

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
        click.echo(f"\n  [buggy FAIL -> fix PASS  (bug-fixing tests)]")
        click.echo(f"    {_fmt_stats(test_stats['fixed_per_bug'])}")
        _dist_bar(test_stats["fixed_distribution"], n_with_cases, "distribution")

        click.echo(f"\n  [FAIL in both states  (pre-existing failures)]")
        click.echo(f"    {_fmt_stats(test_stats['preexisting_per_bug'])}")
        _dist_bar(test_stats["preexisting_distribution"], n_with_cases, "distribution")

        if any(v > 0 for v in test_stats["regression_per_bug"].values()):
            click.echo(f"\n  [buggy PASS -> fix FAIL  (regressions)]")
            click.echo(f"    {_fmt_stats(test_stats['regression_per_bug'])}")

    click.echo("")


@cli.command()
@click.option(
    "--languages", default="rust,java",
    help="Comma-separated languages to compare (default: rust,java)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
@click.pass_context
def compare(ctx: click.Context, languages: str, as_json: bool) -> None:
    """Compare testing-quality metrics across languages (e.g. rust vs java).

    Metrics per language:
      - Validation status breakdown (valid/invalid/setup_failed/patch_failed/
        timeout/error/skipped) - setup_failed in particular flags environment/
        build issues, useful when comparing execution reliability across languages
      - PR test-change rate: % of fix PRs that touch a test file at all
      - test lines added / test files changed per PR
      - functions changed / tests added per PR, and tests-added-per-function-changed
        - how much developers rely on new tests (vs. just editing existing ones)
          to justify a fix, relative to how much source code the fix touched
      - diff coverage: % of the bug's changed lines that existing tests
        (at the buggy commit) actually exercised - requires coverage.enabled
      - overall line/branch coverage of the project at the buggy commit

    \b
    Example:
      bp compare
      bp compare --languages rust,java --json
    """
    import json
    import statistics

    from bug_prospector.adapters.language import get_language_adapter
    from bug_prospector.core.models.record import MEASURED, SETUP_FAILED, SKIPPED
    from bug_prospector.core.storage import Storage
    from bug_prospector.core.test_effort import compute as compute_test_effort

    dataset_dir: Path = ctx.obj["dataset_dir"]
    storage = Storage(dataset_dir)
    all_records = storage.list_all()

    langs = [l.strip() for l in languages.split(",") if l.strip()]

    def _stats(vals: list[float]) -> dict:
        if not vals:
            return {}
        return {
            "n": len(vals),
            "mean": round(sum(vals) / len(vals), 3),
            "median": round(statistics.median(vals), 3),
            "min": round(min(vals), 3),
            "max": round(max(vals), 3),
        }

    report: dict[str, dict] = {}

    for lang in langs:
        records = [
            r for r in all_records
            if (r.reproduction.environment.language or "").lower() == lang.lower()
        ]
        n = len(records)
        adapter = get_language_adapter(lang)
        patterns = adapter.default_test_patterns() if adapter else []

        touched_tests = 0
        test_lines_added: list[float] = []
        test_files_changed: list[float] = []
        functions_changed: list[float] = []
        tests_added: list[float] = []
        tests_added_per_function: list[float] = []
        for r in records:
            effort = compute_test_effort(r.fix.diff, patterns, lang)
            if effort.test_files_changed > 0:
                touched_tests += 1
            test_lines_added.append(effort.test_lines_added)
            test_files_changed.append(effort.test_files_changed)
            functions_changed.append(effort.functions_changed)
            tests_added.append(effort.tests_added)
            if effort.tests_added_per_function is not None:
                tests_added_per_function.append(effort.tests_added_per_function)

        cov_records = [
            r for r in records
            if r.validation and r.validation.coverage and r.validation.coverage.status == MEASURED
        ]
        diff_cov = [
            r.validation.coverage.diff_coverage_rate for r in cov_records
            if r.validation.coverage.diff_coverage_rate is not None
        ]
        overall_line = [
            r.validation.coverage.overall_line_rate for r in cov_records
            if r.validation.coverage.overall_line_rate is not None
        ]
        overall_branch = [
            r.validation.coverage.overall_branch_rate for r in cov_records
            if r.validation.coverage.overall_branch_rate is not None
        ]
        zero_diff_cov = sum(1 for v in diff_cov if v == 0.0)

        val_counts: dict[str, int] = {}
        for r in records:
            s = r.validation.status if r.validation else SKIPPED
            val_counts[s] = val_counts.get(s, 0) + 1

        report[lang] = {
            "total_bugs": n,
            "validation_status": val_counts,
            "pr_test_change_rate": round(touched_tests / n, 3) if n else None,
            "test_lines_added_per_pr": _stats(test_lines_added),
            "test_files_changed_per_pr": _stats(test_files_changed),
            "test_discipline": {
                "functions_changed_per_pr": _stats(functions_changed),
                "tests_added_per_pr": _stats(tests_added),
                "prs_with_new_tests_rate": (
                    round(sum(1 for v in tests_added if v > 0) / n, 3) if n else None
                ),
                "tests_added_per_function_changed": _stats(tests_added_per_function),
            },
            "coverage": {
                "n_measured": len(cov_records),
                "diff_coverage_rate": _stats(diff_cov),
                "diff_coverage_zero_rate": round(zero_diff_cov / len(diff_cov), 3) if diff_cov else None,
                "overall_line_rate": _stats(overall_line),
                "overall_branch_rate": _stats(overall_branch),
            },
        }

    if as_json:
        click.echo(json.dumps(report, ensure_ascii=False, indent=2))
        return

    click.echo(f"\n{'='*72}")
    click.echo(f"  Language comparison: {' vs '.join(langs)}")
    click.echo(f"{'='*72}")

    for lang in langs:
        d = report[lang]
        click.echo(f"\n[{lang}]  (total bugs: {d['total_bugs']})")
        if d["total_bugs"] == 0:
            click.echo("  no data collected yet")
            continue

        val_counts = d["validation_status"]
        if val_counts:
            breakdown = ", ".join(
                f"{s}={n}" for s, n in sorted(val_counts.items(), key=lambda x: -x[1])
            )
            click.echo(f"  validation status      : {breakdown}")
            setup_failed_n = val_counts.get(SETUP_FAILED, 0)
            if setup_failed_n:
                click.echo(
                    f"    -> setup_failed (environment/build failure): "
                    f"{setup_failed_n} ({setup_failed_n / d['total_bugs'] * 100:.1f}%)"
                )

        pct = d["pr_test_change_rate"]
        click.echo(f"  PR touches a test file : {pct*100:5.1f}%" if pct is not None else "  n/a")
        tla = d["test_lines_added_per_pr"]
        if tla:
            click.echo(f"  test lines added/PR    : mean={tla['mean']}  median={tla['median']}  max={tla['max']}")

        disc = d["test_discipline"]
        fc, ta = disc["functions_changed_per_pr"], disc["tests_added_per_pr"]
        if fc:
            click.echo(f"  functions changed/PR   : mean={fc['mean']}  median={fc['median']}  max={fc['max']}")
        if ta:
            click.echo(f"  new tests added/PR     : mean={ta['mean']}  median={ta['median']}  max={ta['max']}")
        rate = disc["prs_with_new_tests_rate"]
        if rate is not None:
            click.echo(f"  PRs adding a new test  : {rate*100:5.1f}%")
        tapf = disc["tests_added_per_function_changed"]
        if tapf:
            click.echo(
                f"  new tests / function changed (PRs w/ changed functions): "
                f"mean={tapf['mean']}  median={tapf['median']}"
            )

        cov = d["coverage"]
        click.echo(f"  coverage measured      : {cov['n_measured']} bugs")
        if cov["diff_coverage_rate"]:
            dc = cov["diff_coverage_rate"]
            click.echo(
                f"  diff coverage (buggy lines covered by existing tests): "
                f"mean={dc['mean']*100:.1f}%  median={dc['median']*100:.1f}%"
            )
            click.echo(f"    -> 0% diff coverage (bug in wholly untested code): {cov['diff_coverage_zero_rate']*100:.1f}%")
        if cov["overall_line_rate"]:
            olr = cov["overall_line_rate"]
            click.echo(f"  overall line coverage  : mean={olr['mean']*100:.1f}%  median={olr['median']*100:.1f}%")

    click.echo("")


@cli.command()
@click.option("--tracker", default=None, help="Filter by tracker URL (prefix match)")
@click.option(
    "--status", "status_filter", default=None,
    help="Filter by validation status (valid/invalid/setup_failed/patch_failed/timeout/error/skipped)",
)
@click.option(
    "--bug-id", "bug_ids", multiple=True,
    help="Filter by exact bug ID (repeatable)",
)
@click.option("--dry-run", is_flag=True, help="Preview only. Nothing is deleted.")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt")
@click.pass_context
def delete(
    ctx: click.Context,
    tracker: str | None,
    status_filter: str | None,
    bug_ids: tuple[str, ...],
    dry_run: bool,
    yes: bool,
) -> None:
    """Delete collected records so they can be re-collected (e.g. after fixing a bug).

    At least one of --tracker / --status / --bug-id is required, as a safety guard
    against accidentally wiping the whole dataset. Also removes the matching
    patch files and test logs under the dataset directory, if any.

    \b
    Examples:
      bp delete --tracker https://github.com/BurntSushi/ripgrep --status error
      bp delete --bug-id github-BurntSushi-ripgrep-3419 --bug-id github-BurntSushi-ripgrep-3180
      bp delete --tracker https://github.com/BurntSushi/ripgrep --status error --dry-run
    """
    from bug_prospector.core.storage import Storage

    if not (tracker or status_filter or bug_ids):
        raise click.UsageError("At least one filter is required: --tracker, --status, or --bug-id")

    dataset_dir: Path = ctx.obj["dataset_dir"]
    storage = Storage(dataset_dir)
    records = storage.list_all()

    def _matches(r) -> bool:
        if tracker and not r.source.tracker_url.startswith(tracker):
            return False
        if status_filter:
            s = r.validation.status if r.validation else "skipped"
            if s != status_filter:
                return False
        if bug_ids and r.id not in bug_ids:
            return False
        return True

    targets = [r for r in records if _matches(r)]

    click.echo(f"{len(targets)} record(s) match the filter.")
    if not targets:
        return

    preview = targets[:20]
    for r in preview:
        s = r.validation.status if r.validation else "skipped"
        click.echo(f"  {r.id}  (validation={s})")
    if len(targets) > len(preview):
        click.echo(f"  ... and {len(targets) - len(preview)} more")

    if dry_run:
        click.echo("[dry-run] Nothing deleted.")
        return

    if not yes and not click.confirm(f"Delete {len(targets)} record(s) from the dataset?"):
        click.echo("Aborted.")
        return

    deleted_files = 0
    for r in targets:
        storage.delete(r.id)
        for pattern in (f"**/{r.id}.patch", f"**/{r.id}-*.log"):
            for p in dataset_dir.glob(pattern):
                p.unlink(missing_ok=True)
                deleted_files += 1

    suffix = f" and {deleted_files} related file(s)" if deleted_files else ""
    click.echo(f"Deleted {len(targets)} record(s){suffix}. Re-run `bp collect` to re-collect them.")


@cli.command(name="export-tests")
@click.option("--tracker", default=None, help="Filter by tracker URL (prefix match)")
@click.option(
    "--bug-id", "bug_ids", multiple=True,
    help="Filter by exact bug ID (repeatable)",
)
@click.option(
    "--out-dir", default=None, type=click.Path(file_okay=False),
    help="Output directory (default: <dataset-dir>/generated-tests)",
)
@click.pass_context
def export_tests(
    ctx: click.Context,
    tracker: str | None,
    bug_ids: tuple[str, ...],
    out_dir: str | None,
) -> None:
    """Export LLM-generated reproduction tests (from `bp generate-tests`) as files.

    Writes each record's reproduction.generated_test.source_code to
    <out-dir>/<bug_id>/<file_path>, plus a meta.json with the issue URL,
    buggy/fix commits, model and attempt count. Existing files are overwritten.

    \b
    Examples:
      bp export-tests
      bp export-tests --tracker https://github.com/clap-rs/clap --out-dir ./clap-tests
      bp export-tests --bug-id github-clap-rs-clap-4326
    """
    from bug_prospector.core.storage import Storage
    from bug_prospector.core.test_generator import export_generated_tests

    dataset_dir: Path = ctx.obj["dataset_dir"]
    target_dir = Path(out_dir) if out_dir else dataset_dir / "generated-tests"

    records = Storage(dataset_dir).list_all()
    if tracker:
        records = [r for r in records if r.source.tracker_url.startswith(tracker)]
    if bug_ids:
        records = [r for r in records if r.id in bug_ids]

    exported = export_generated_tests(records, target_dir)
    click.echo(f"Exported {len(exported)} generated test(s) to {target_dir}")
    for r, path in exported:
        g = r.reproduction.generated_test
        status = "confirmed" if g.confirmed else "unconfirmed"
        click.echo(f"  {r.id}  ({status}, {g.attempts} attempt(s))  {path.relative_to(target_dir)}")


@cli.command(name="generate-tests")
@click.option(
    "--project", "-p",
    "project_file",
    required=True,
    type=click.Path(exists=True),
    help="Per-project YAML config file (must include an `llm:` block; see doc/test-generation.md)",
)
@click.option(
    "--tracker", default=None,
    help="Filter by tracker URL (prefix match; defaults to the project's tracker_url)",
)
@click.option(
    "--status", "status_filter", default=None,
    help="Only target records currently at this validation status "
         "(default: any status without a confirmed reproduction, e.g. invalid/setup_failed/...)",
)
@click.option(
    "--bug-id", "bug_ids", multiple=True,
    help="Filter by exact bug ID (repeatable)",
)
@click.option("--limit", type=int, default=None, help="Max candidate records to process")
@click.option("--max-attempts", type=int, default=None, help="Override llm.max_attempts from the config")
@click.option("--dry-run", is_flag=True, help="List candidates only. No LLM calls are made.")
@click.pass_context
def generate_tests(
    ctx: click.Context,
    project_file: str,
    tracker: str | None,
    status_filter: str | None,
    bug_ids: tuple[str, ...],
    limit: int | None,
    max_attempts: int | None,
    dry_run: bool,
) -> None:
    """Generate reproduction tests for bugs whose fix has no accompanying test.

    Targets records whose fix diff contains no test-file changes and whose
    validation hasn't confirmed a FAIL->PASS reproduction yet (see
    doc/test-generation.md). Uses the project's `llm:` config to synthesize a
    new test, then verifies it mechanically (fails on the buggy commit, passes
    on the fix commit) using the same worktree/reverse-patch machinery as
    `bp collect`'s validation step. Confirmed records are upgraded to
    validation.status="llm_valid" and keep the generated test source.
    Records that fail to produce a confirmed test are left untouched and are
    retried on the next run (there is currently no persisted "already tried
    and failed" state).

    \b
    Examples:
      bp generate-tests --project flask.yaml --dry-run
      bp generate-tests --project flask.yaml --limit 5
      bp generate-tests --project flask.yaml --bug-id github-pallets-flask-1234
    """
    from bug_prospector.adapters.llm import get_llm_adapter
    from bug_prospector.adapters.tracker.github import GitHubAdapter
    from bug_prospector.adapters.vcs.git import GitAdapter
    from bug_prospector.config import ProjectConfig
    from bug_prospector.core.storage import Storage
    from bug_prospector.core.test_generator import TestGenerator, is_candidate
    from bug_prospector.core.validator import Validator

    dataset_dir: Path = ctx.obj["dataset_dir"]
    project = ProjectConfig.from_file(Path(project_file))

    if not project.llm.enabled:
        raise click.UsageError(
            "llm.enabled is false in the project config. Set `llm: {enabled: true}` to use this command."
        )
    if max_attempts is not None:
        project.llm.max_attempts = max_attempts

    storage = Storage(dataset_dir)
    all_records = storage.list_all()

    tracker_prefix = tracker or project.tracker_url
    records = [r for r in all_records if r.source.tracker_url.startswith(tracker_prefix)]
    if status_filter:
        records = [r for r in records if (r.validation.status if r.validation else "skipped") == status_filter]
    if bug_ids:
        records = [r for r in records if r.id in bug_ids]

    candidates = [r for r in records if is_candidate(r, project.language.test_patterns)]
    if limit is not None:
        candidates = candidates[:limit]

    click.echo(f"{len(candidates)} candidate record(s) found (fix has no accompanying test, not yet confirmed).")
    if not candidates:
        return

    preview = candidates[:20]
    for r in preview:
        s = r.validation.status if r.validation else "skipped"
        click.echo(f"  {r.id}  (validation={s})")
    if len(candidates) > len(preview):
        click.echo(f"  ... and {len(candidates) - len(preview)} more")

    if dry_run:
        click.echo("[dry-run] No LLM calls made.")
        return

    slug = GitHubAdapter.parse_slug(project.tracker_url)
    repo_slug_flat = slug.replace("/", "-")
    local_repo = dataset_dir / "repos" / repo_slug_flat
    GitAdapter().ensure_repo(project.repo_url, local_repo)

    worktree_path = dataset_dir / "worktrees" / repo_slug_flat
    validator = Validator(project.language, project.validation, worktree_path=worktree_path)
    usage_log_path = dataset_dir / "llm-usage" / f"{project.project_id}.jsonl"
    llm = get_llm_adapter(project.llm, usage_log_path=usage_log_path)
    generator = TestGenerator(
        storage=storage, validator=validator, llm=llm,
        llm_config=project.llm, lang_config=project.language,
    )

    click.echo(f"\nGenerating reproduction tests for {len(candidates)} candidate record(s)...")
    try:
        confirmed, failed, _skipped = generator.run(local_repo, candidates)
    finally:
        validator.close()
        _print_llm_usage(getattr(llm, "usage_records", []), getattr(generator, "outcomes", {}), usage_log_path)

    click.echo(f"\nGenerated: {confirmed} confirmed  |  {failed} failed")


def _print_llm_usage(usage_records: list[dict], outcomes: dict[str, tuple[bool, int]], log_path: Path) -> None:
    """bp generate-tests 実行後に、レコード単位のトークン使用量・コストを表示する。"""
    if not usage_records:
        return
    keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    per_bug: dict[str, dict] = {}
    for u in usage_records:
        agg = per_bug.setdefault(u["bug_id"], {"calls": 0, "cost": 0.0, "turns": 0, **{k: 0 for k in keys}})
        agg["calls"] += 1
        agg["cost"] += u.get("total_cost_usd") or 0.0
        agg["turns"] += u.get("num_turns") or 0
        for k in keys:
            agg[k] += u.get(k) or 0

    click.echo("\nLLM usage (per record):")
    header = ("bug_id", "result", "calls", "turns", "input", "output", "cache_w", "cache_r", "cost_usd")
    fmt = "  {:<36} {:<10} {:>5} {:>5} {:>8} {:>8} {:>10} {:>11} {:>9}"
    click.echo(fmt.format(*header))
    total = {"calls": 0, "cost": 0.0, "turns": 0, **{k: 0 for k in keys}}
    for bug_id, agg in per_bug.items():
        ok, _ = outcomes.get(bug_id, (False, 0))
        click.echo(fmt.format(
            bug_id, "confirmed" if ok else "failed", agg["calls"], agg["turns"],
            *(agg[k] for k in keys), f"{agg['cost']:.4f}",
        ))
        for k in total:
            total[k] += agg[k]
    click.echo(fmt.format("TOTAL", "", total["calls"], total["turns"], *(total[k] for k in keys), f"{total['cost']:.4f}"))
    click.echo(f"  (raw per-call log: {log_path})")


if __name__ == "__main__":
    cli()
