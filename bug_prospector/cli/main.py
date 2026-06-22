from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import click


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


if __name__ == "__main__":
    cli()
