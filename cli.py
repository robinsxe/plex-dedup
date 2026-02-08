#!/usr/bin/env python3
"""
CLI interface for Plex Dedup.
Run from terminal for a quick, no-UI workflow.
"""

import sys
import argparse
import logging

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Confirm
from rich import box
import humanize

from config import Config
from dedup_engine import DedupEngine
from subtitle_manager import SubtitleManager

console = Console()


def setup_logging(verbose: bool):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")


def print_banner():
    console.print(Panel.fit(
        "[bold yellow]🎬 Plex Dedup[/] — Media Duplicate & Subtitle Manager",
        border_style="yellow",
    ))


def cmd_scan(engine, config, args, media_type="movie"):
    """Scan for duplicates and display results."""
    if media_type == "all":
        console.print("\n[bold]Scanning all libraries...[/]")
        with console.status("[bold yellow]Scanning..."):
            plans = engine.scan_all()
    elif media_type == "show":
        lib = args.library or config.plex_tv_library
        console.print(f"\n[bold]Scanning TV library:[/] {lib}")
        with console.status("[bold yellow]Scanning TV library..."):
            plans = engine.scan(lib, "show")
    else:
        lib = args.library or config.plex_movie_library
        console.print(f"\n[bold]Scanning movie library:[/] {lib}")
        with console.status("[bold yellow]Scanning movie library..."):
            plans = engine.scan(lib, "movie")

    if not plans:
        console.print("\n[bold green]✅ No duplicates found![/]")
        return None

    summary = engine.get_summary(plans)
    console.print()
    table = Table(title="Scan Summary", box=box.ROUNDED)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right", style="yellow")
    table.add_row("Total duplicates", str(summary["total_duplicates"]))
    table.add_row("Movie duplicates", str(summary["movie_duplicates"]))
    table.add_row("Episode duplicates", str(summary["episode_duplicates"]))
    table.add_row("Files to remove", str(summary["total_files_to_remove"]))
    table.add_row("Space to reclaim", f"{summary['total_space_saved_gb']} GB")
    table.add_row("Found in Radarr/Sonarr", str(summary["arr_found"]))
    table.add_row("Strategy", summary["keep_strategy"])
    table.add_row("Dry run", "Yes ✅" if config.dry_run else "[red]No ⚠️[/]")
    console.print(table)

    console.print()
    detail = Table(title="Duplicates", box=box.SIMPLE_HEAVY, show_lines=True)
    detail.add_column("#", justify="right", style="dim", width=4)
    detail.add_column("Title", style="bold", max_width=45)
    detail.add_column("Type", justify="center", width=6)
    detail.add_column("Copies", justify="center")
    detail.add_column("Keep", style="green", max_width=40)
    detail.add_column("Savings", justify="right", style="yellow")
    detail.add_column("Arr", justify="center")

    for i, plan in enumerate(plans, 1):
        pd = plan.to_dict()
        mtype = "📺 TV" if plan.group.media_type == "episode" else "🎬 Mov"
        keep_info = f"{plan.keep.resolution} / {plan.keep.video_codec}\n{plan.keep.file_size_gb} GB"
        arr = "✅" if pd["arr_found"] else "❌"
        detail.add_row(str(i), pd["display_title"], mtype, str(pd["file_count"]), keep_info, f"{pd['space_saved_gb']} GB", arr)

    console.print(detail)
    return plans


def cmd_execute(engine, config, plans, args):
    if not plans:
        console.print("[yellow]Nothing to execute.[/]")
        return
    if config.dry_run:
        console.print("\n[bold yellow]🔍 DRY RUN MODE[/]")
    else:
        total_gb = engine.get_summary(plans)['total_space_saved_gb']
        console.print(f"\n[bold red]⚠️  LIVE MODE[/] — Will delete {sum(len(p.remove) for p in plans)} files, reclaiming {total_gb} GB")
        if not args.yes and not Confirm.ask("Continue?"):
            console.print("[dim]Cancelled.[/]")
            return
    with console.status("[bold yellow]Executing..."):
        result = engine.execute_all(plans)
    console.print(f"\n[bold green]✅ Done![/] {result['success']} succeeded, {result['failed']} failed")


def cmd_subtitles(config, args):
    """Scan and download missing subtitles."""
    console.print("\n[bold]Subtitle Sync[/]")
    mgr = SubtitleManager(config)
    conn = mgr.test_connections()

    if not conn["plex"]:
        console.print("[bold red]Cannot connect to Plex![/]")
        return
    if not conn["opensubtitles"]:
        console.print("[bold red]Cannot connect to OpenSubtitles![/]")
        return
    if not conn["opensubtitles_login"]:
        console.print("[bold yellow]OpenSubtitles login failed — downloads won't work[/]")

    langs = config.subtitle_languages
    console.print(f"Languages: [bold]{', '.join(langs)}[/]")
    results = []

    if args.type in ("movies", "all"):
        console.print(f"\n[bold]Scanning movie library:[/] {config.plex_movie_library}")
        with console.status("[bold yellow]Processing movie subtitles..."):
            res = mgr.download_subtitles(
                config.plex_movie_library, "movie", langs,
                dry_run=config.dry_run, limit=args.sub_limit,
            )
            results.extend(res)

    if args.type in ("tv", "all"):
        console.print(f"\n[bold]Scanning TV library:[/] {config.plex_tv_library}")
        with console.status("[bold yellow]Processing TV subtitles..."):
            res = mgr.download_subtitles(
                config.plex_tv_library, "show", langs,
                dry_run=config.dry_run, limit=args.sub_limit,
            )
            results.extend(res)

    summary = mgr.get_summary(results)
    console.print()
    table = Table(title="Subtitle Summary", box=box.ROUNDED)
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right", style="yellow")
    table.add_row("Items processed", str(summary["total_items_processed"]))
    table.add_row("Subtitles downloaded", str(summary["subtitles_downloaded"]))
    table.add_row("Found (dry run)", str(summary["subtitles_found_dry_run"]))
    table.add_row("Not available", str(summary["subtitles_not_available"]))
    table.add_row("Already exist", str(summary["subtitles_already_exist"]))
    console.print(table)


def main():
    parser = argparse.ArgumentParser(description="Plex Dedup — Media Manager")
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # Dedup command (default)
    dedup_p = sub.add_parser("dedup", help="Find and remove duplicates")
    dedup_p.add_argument("-l", "--library", help="Plex library name")
    dedup_p.add_argument("--type", choices=["movies", "tv", "all"], default="all", help="What to scan")
    dedup_p.add_argument("--strategy", choices=["best_quality", "largest_file", "newest"])
    dedup_p.add_argument("--live", action="store_true", help="Disable dry run")
    dedup_p.add_argument("--no-unmonitor", action="store_true")
    dedup_p.add_argument("-y", "--yes", action="store_true")
    dedup_p.add_argument("--scan-only", action="store_true")

    # Subtitles command
    sub_p = sub.add_parser("subtitles", help="Download missing subtitles")
    sub_p.add_argument("--type", choices=["movies", "tv", "all"], default="all")
    sub_p.add_argument("--live", action="store_true", help="Disable dry run")
    sub_p.add_argument("--limit", dest="sub_limit", type=int, default=50, help="Max items to process")

    # Web command
    web_p = sub.add_parser("web", help="Launch web dashboard")

    # Global
    parser.add_argument("-v", "--verbose", action="store_true")
    # Also support old-style flags for backward compat
    parser.add_argument("--web", action="store_true", help="Launch web dashboard")
    parser.add_argument("--live", action="store_true", help="Disable dry run")
    parser.add_argument("-y", "--yes", action="store_true")

    args = parser.parse_args()
    setup_logging(args.verbose if hasattr(args, 'verbose') else False)
    print_banner()

    config = Config.from_env()
    if getattr(args, 'strategy', None):
        config.keep_strategy = args.strategy
    if getattr(args, 'live', False):
        config.dry_run = False
    if getattr(args, 'no_unmonitor', False):
        config.auto_unmonitor = False

    errors = config.validate()
    if errors:
        console.print("[bold red]Configuration errors:[/]")
        for e in errors:
            console.print(f"  [red]• {e}[/]")
        console.print("\nCreate a [bold].env[/] file — see README.")
        sys.exit(1)

    # Web mode
    if args.command == "web" or getattr(args, 'web', False):
        console.print("[bold]Starting web dashboard on port {0}...[/]".format(config.web_port))
        from app import run
        run()
        return

    # Subtitles mode
    if args.command == "subtitles":
        cmd_subtitles(config, args)
        return

    # Default: dedup
    engine = DedupEngine(config)
    console.print("[dim]Testing connections...[/]")
    conn = engine.test_connections()
    if not conn["plex"]:
        console.print("[bold red]Cannot connect to Plex![/]")
        sys.exit(1)
    if not conn["radarr"]:
        console.print("[bold yellow]⚠ Radarr not available[/]")
    if not conn["sonarr"]:
        console.print("[bold yellow]⚠ Sonarr not available[/]")

    scan_type = getattr(args, 'type', 'all') or 'all'
    type_map = {"movies": "movie", "tv": "show", "all": "all"}
    plans = cmd_scan(engine, config, args, type_map.get(scan_type, "all"))

    if plans and not getattr(args, 'scan_only', False):
        cmd_execute(engine, config, plans, args)


if __name__ == "__main__":
    main()
