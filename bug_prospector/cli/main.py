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
@click.option(
    "--source", "sources", multiple=True,
    type=click.Choice(["issues", "pull_requests", "commits"]),
    help="Override sources.*.enabled: collect only from these sources (repeatable)",
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
    sources: tuple[str, ...],
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
      bp collect --project serde.yaml --source pull_requests --source commits
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

    if sources:
        for name in ("issues", "pull_requests", "commits"):
            getattr(project.sources, name).enabled = name in sources

    if dry_run:
        click.echo("[dry-run] No data will be saved.")
    click.echo(f"Collecting bugs from {project.tracker_url} ...")
    if project.validation.enabled:
        click.echo(f"  Validation enabled  (strategy={project.validation.strategy})")
    enabled_sources = [n for n in ("issues", "pull_requests", "commits") if getattr(project.sources, n).enabled]
    click.echo(f"  Sources: {', '.join(enabled_sources) or '(none)'}")
    for name in enabled_sources:
        checks = getattr(project.sources, name).fix_checks
        modes = {n: checks.mode(n) for n in ("revert", "follow_up_fix", "release", "ci") if checks.mode(n) != "off"}
        if modes:
            click.echo(f"    {name} fix checks: " + ", ".join(f"{k}={v}" for k, v in modes.items()))
    if project.sources.issues.enabled and not project.search.labels:
        click.echo("  No label filter: every closed issue with an identifiable fix is considered")
    if project.llm.classify_bugs:
        click.echo("  LLM bug classification enabled  (non-bugs are recorded as validation=not_bug)"
                   + ("  [skipped in dry-run]" if dry_run else ""))

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

    def _echo_llm_cost() -> None:
        usage = getattr(getattr(collector, "classifier", None), "usage_records", [])
        if usage:
            cost = sum(u.get("total_cost_usd") or 0.0 for u in usage)
            click.echo(f"  LLM bug classification: {len(usage)} call(s), ${cost:.4f}")

    try:
        new, skipped, failed, validation_counts = collector.run_project(
            project=project,
            limit_override=limit,
            dry_run=dry_run,
        )
        click.echo(f"\nCollected: {new} new  |  {skipped} skipped  |  {failed} failed")
        _echo_validation_counts(validation_counts)
        if len(collector.source_counts) > 1:
            for name, t in collector.source_counts.items():
                click.echo(f"  [{name}] {t.new} new  |  {t.skipped} skipped  |  {t.failed} failed")
        _echo_llm_cost()
    except CollectionInterrupted as e:
        click.echo(
            f"\n[interrupted] Collected so far: {e.new} new  |  {e.skipped} skipped  |  {e.failed} failed"
        )
        _echo_validation_counts(e.validation_counts)
        _echo_llm_cost()
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
    from bug_prospector.core.models.record import PATCH_FAILED, SETUP_FAILED, TIMEOUT, ERROR, SKIPPED

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

    # --- LLM bug classification (bp collect, llm.classify_bugs) ---
    # --- source kind（出自: issue / pull_request / commit）と、出自ごとの validation status ---
    kind_counts: dict[str, int] = {}
    status_by_kind: dict[str, dict[str, int]] = {}
    for r in records:
        kind = getattr(r.source, "kind", "issue")
        kind_counts[kind] = kind_counts.get(kind, 0) + 1
        s = r.validation.status if r.validation else SKIPPED
        per = status_by_kind.setdefault(kind, {})
        per[s] = per.get(s, 0) + 1

    # --- fix checks（doc/fix-checks.md）: 確認ごとの pass / fail / unknown ---
    check_counts: dict[str, dict[str, int]] = {}
    for r in records:
        for name, result in (r.fix_checks or {}).items():
            outcome = {True: "pass", False: "fail", None: "unknown"}[result.passed]
            per = check_counts.setdefault(name, {"pass": 0, "fail": 0, "unknown": 0})
            per[outcome] += 1

    classification_counts: dict[str, int] = {}
    for r in records:
        c = r.bug_classification
        if c is None:
            continue
        key = "bug" if c.is_bug else f"not a bug: {c.category}"
        classification_counts[key] = classification_counts.get(key, 0) + 1

    # --- LLM testability screening (bp generate-tests, llm.screening) ---
    screening_counts: dict[str, int] = {}
    for r in records:
        t = r.reproduction.testability
        if t is None:
            continue
        key = "testable" if t.testable else f"not testable: {t.category}"
        screening_counts[key] = screening_counts.get(key, 0) + 1

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
        "by_source_kind": kind_counts,
        "validation_by_source_kind": status_by_kind,
        "fix_checks": check_counts,
        "bug_classification": classification_counts,
        "testability_screening": screening_counts,
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

    if set(kind_counts) - {"issue"}:
        click.echo("\n[Source kind]  (issue: independent bug report / pull_request: PR only / commit: direct commit)")
        _bar(kind_counts, total)
        click.echo("\n[Validation status by source kind]")
        for kind, per in status_by_kind.items():
            breakdown = ", ".join(f"{s}={n}" for s, n in sorted(per.items(), key=lambda x: -x[1]))
            click.echo(f"  {kind:<14} {breakdown}")

    if check_counts:
        click.echo("\n[Fix checks]  (pass / fail / unknown)")
        for name, per in check_counts.items():
            click.echo(f"  {name:<15} {per['pass']:>5} / {per['fail']:>5} / {per['unknown']:>5}")

    if classification_counts:
        n_classified = sum(classification_counts.values())
        click.echo(f"\n[LLM bug classification]  (classified: {n_classified})")
        _bar(classification_counts, n_classified)

    if screening_counts:
        n_screened = sum(screening_counts.values())
        click.echo(f"\n[LLM testability screening]  (screened: {n_screened})")
        _bar(screening_counts, n_screened)

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
@click.option(
    "--kind", "kinds", multiple=True,
    type=click.Choice(["issue", "pull_request", "commit"]),
    help="Only target records of these source kinds (repeatable). "
         "Without an independent bug report (pull_request/commit), a generated test can only "
         "reflect the fix's own behaviour, so llm_valid there is weaker evidence.",
)
@click.option("--limit", type=int, default=None, help="Max candidate records to process")
@click.option("--max-attempts", type=int, default=None, help="Override llm.max_attempts from the config")
@click.option(
    "--screen-only", is_flag=True,
    help="Only run the testability screening (requires llm.screening: true); do not generate tests.",
)
@click.option(
    "--rescreen", is_flag=True,
    help="Discard previous screening verdicts of the selected records and ask the LLM again.",
)
@click.option("--dry-run", is_flag=True, help="List candidates only. No LLM calls are made.")
@click.pass_context
def generate_tests(
    ctx: click.Context,
    project_file: str,
    tracker: str | None,
    status_filter: str | None,
    bug_ids: tuple[str, ...],
    kinds: tuple[str, ...],
    limit: int | None,
    max_attempts: int | None,
    screen_only: bool,
    rescreen: bool,
    dry_run: bool,
) -> None:
    """Generate reproduction tests for bugs whose fix has no accompanying test.

    Targets records whose fix diff contains no test-file changes and whose
    validation hasn't confirmed a FAIL->PASS reproduction yet (see
    doc/test-generation.md). With `llm.include_fixes_with_tests: true`, fixes
    that do include tests are also targeted as long as those tests didn't
    confirm FAIL->PASS. Uses the project's `llm:` config to synthesize a
    new test, then verifies it mechanically (fails on the buggy commit, passes
    on the fix commit) using the same worktree/reverse-patch machinery as
    `bp collect`'s validation step. Confirmed records are upgraded to
    validation.status="llm_valid" and keep the generated test source.
    Records that fail to produce a confirmed test are left untouched and are
    retried on the next run (there is currently no persisted "already tried
    and failed" state).

    With `llm.screening: true`, each candidate is first screened: the LLM is
    asked whether the bug can be reproduced by a test at all (e.g. it is not
    caused by the program itself, or depends on the environment). Records
    judged untestable are excluded, and the verdict is stored in
    reproduction.testability so they stay excluded on later runs
    (use --rescreen to ask again).

    \b
    Examples:
      bp generate-tests --project flask.yaml --dry-run
      bp generate-tests --project flask.yaml --limit 5
      bp generate-tests --project flask.yaml --bug-id github-pallets-flask-1234
      bp generate-tests --project clap.yaml --screen-only
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
    if (screen_only or rescreen) and not project.llm.screening:
        raise click.UsageError("--screen-only/--rescreen require `llm.screening: true` in the project config.")

    storage = Storage(dataset_dir)
    all_records = storage.list_all()

    tracker_prefix = tracker or project.tracker_url
    records = [r for r in all_records if r.source.tracker_url.startswith(tracker_prefix)]
    if status_filter:
        records = [r for r in records if (r.validation.status if r.validation else "skipped") == status_filter]
    if bug_ids:
        records = [r for r in records if r.id in bug_ids]
    if kinds:
        records = [r for r in records if getattr(r.source, "kind", "issue") in kinds]
    if rescreen:
        # メモリ上でだけ判定結果を消す。再判定に成功したレコードは TestGenerator が上書き保存する
        for r in records:
            r.reproduction.testability = None

    screened_out = [
        r for r in records
        if r.reproduction.testability and not r.reproduction.testability.testable
    ]
    candidates = [
        r for r in records
        if is_candidate(r, project.language.test_patterns, project.llm.include_fixes_with_tests)
    ]
    if limit is not None:
        candidates = candidates[:limit]

    criterion = (
        "no valid accompanying test" if project.llm.include_fixes_with_tests
        else "fix has no accompanying test"
    )
    click.echo(f"{len(candidates)} candidate record(s) found ({criterion}, not yet confirmed).")
    if screened_out:
        click.echo(f"  ({len(screened_out)} record(s) excluded earlier by screening as not testable)")
    if not candidates:
        return

    preview = candidates[:20]
    for r in preview:
        s = r.validation.status if r.validation else "skipped"
        t = r.reproduction.testability
        screening = "" if not project.llm.screening else (
            "  screening=pending" if t is None else "  screening=testable"
        )
        click.echo(f"  {r.id}  (validation={s}){screening}")
    if len(candidates) > len(preview):
        click.echo(f"  ... and {len(candidates) - len(preview)} more")

    if dry_run:
        click.echo("[dry-run] No LLM calls made.")
        return

    slug = GitHubAdapter.parse_slug(project.tracker_url)
    repo_slug_flat = slug.replace("/", "-")
    local_repo = dataset_dir / "repos" / repo_slug_flat
    if not screen_only:  # 生成可否の判定はIssue・diffだけで行うため、クローンは不要
        GitAdapter().ensure_repo(project.repo_url, local_repo)

    worktree_path = dataset_dir / "worktrees" / repo_slug_flat
    validator = Validator(project.language, project.validation, worktree_path=worktree_path)
    usage_log_path = dataset_dir / "llm-usage" / f"{project.project_id}.jsonl"
    llm = get_llm_adapter(project.llm, usage_log_path=usage_log_path)
    generator = TestGenerator(
        storage=storage, validator=validator, llm=llm,
        llm_config=project.llm, lang_config=project.language,
    )

    if screen_only:
        click.echo(f"\nScreening {len(candidates)} candidate record(s) for testability...")
    else:
        click.echo(f"\nGenerating reproduction tests for {len(candidates)} candidate record(s)...")
    try:
        confirmed, failed, _skipped, excluded = generator.run(local_repo, candidates, screen_only=screen_only)
    finally:
        validator.close()
        _print_llm_usage(getattr(llm, "usage_records", []), getattr(generator, "outcomes", {}), usage_log_path)

    if screen_only:
        click.echo(f"\nScreening: {excluded} excluded as not testable  |  {failed} screening error(s)")
    else:
        click.echo(f"\nGenerated: {confirmed} confirmed  |  {failed} failed  |  {excluded} excluded by screening")


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
        if bug_id in outcomes:
            result = "confirmed" if outcomes[bug_id][0] else "failed"
        else:
            result = "screened"  # 生成まで進まなかった（判定のみ）
        click.echo(fmt.format(
            bug_id, result, agg["calls"], agg["turns"],
            *(agg[k] for k in keys), f"{agg['cost']:.4f}",
        ))
        for k in total:
            total[k] += agg[k]
    click.echo(fmt.format("TOTAL", "", total["calls"], total["turns"], *(total[k] for k in keys), f"{total['cost']:.4f}"))
    click.echo(f"  (raw per-call log: {log_path})")


if __name__ == "__main__":
    cli()
