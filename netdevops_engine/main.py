#!/usr/bin/env python3
"""
NetDevOps Engine — CLI entry point.

Usage examples
--------------
    python main.py                       # run 'show version' on all hosts
    python main.py -c "show ip int br"   # run a custom read-only command
    python main.py -g core -v            # target the 'core' group, verbose
    python main.py --validate-only       # just validate inventory + env
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.traceback import install as install_rich_traceback

from core.engine import (
    DEFAULT_INVENTORY_DIR,
    ExecutionReport,
    MissingCredentialsError,
    init_engine,
    run_show_version,
)
from core.models import Inventory

console = Console()
err_console = Console(stderr=True, style="bold red")
install_rich_traceback(show_locals=False, suppress=[])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netdevops-engine",
        description="Async network orchestration engine (Nornir + scrapli).",
    )
    parser.add_argument(
        "-c",
        "--command",
        default="show version",
        help="Read-only CLI command to execute on all targets "
             "(default: 'show version').",
    )
    parser.add_argument(
        "-g",
        "--group",
        default=None,
        help="Restrict execution to hosts in this Nornir group.",
    )
    parser.add_argument(
        "-i",
        "--inventory",
        type=Path,
        default=DEFAULT_INVENTORY_DIR,
        help=f"Path to inventory directory (default: {DEFAULT_INVENTORY_DIR}).",
    )
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=10,
        help="Number of concurrent workers (default: 10).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate inventory and credentials, then exit without "
             "connecting to devices.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Print full raw device output for every host.",
    )
    return parser


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_inventory(inv: Inventory) -> None:
    table = Table(
        title="Validated Inventory",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("Host", style="bold")
    table.add_column("Mgmt IP")
    table.add_column("Platform")
    table.add_column("Groups")
    for name, host in inv.hosts.items():
        platform = inv.resolved_platform(name).value
        table.add_row(
            name,
            str(host.hostname),
            platform,
            ", ".join(host.groups) or "-",
        )
    console.print(table)


def render_report(report: ExecutionReport, verbose: bool) -> None:
    console.print(Rule(f"[bold]Results — [cyan]{report.command}[/cyan]"))

    table = Table(show_lines=False, header_style="bold magenta")
    table.add_column("Host", style="bold")
    table.add_column("Mgmt IP")
    table.add_column("Platform")
    table.add_column("Status", justify="center")
    table.add_column("Summary", overflow="fold")

    for r in report.results:
        if r.succeeded:
            status = "[bold green]OK[/bold green]"
            first_line = r.output.strip().splitlines()[0] if r.output.strip() else ""
            summary = first_line[:80]
        else:
            status = "[bold red]FAIL[/bold red]"
            summary = f"[red]{r.error.splitlines()[0] if r.error else 'unknown error'}[/red]"
        table.add_row(r.host, r.hostname, r.platform, status, summary)

    console.print(table)

    ok_style = "bold green" if report.all_succeeded else "bold yellow"
    console.print(
        f"\n[{ok_style}]Succeeded: {report.ok_count}[/{ok_style}]   "
        f"[bold red]Failed: {report.failed_count}[/bold red]   "
        f"Total: {len(report.results)}\n"
    )

    if verbose:
        for r in report.results:
            title = f"{r.host} ({r.hostname})"
            if r.succeeded:
                body = Syntax(
                    r.output or "(no output)",
                    "text",
                    theme="ansi_dark",
                    word_wrap=True,
                )
                console.print(Panel(body, title=title, border_style="green"))
            else:
                console.print(
                    Panel(
                        r.error or "(no error detail)",
                        title=title,
                        border_style="red",
                    )
                )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    console.print(
        Panel.fit(
            "[bold cyan]NetDevOps Engine[/bold cyan]\n"
            "Async network orchestration — Nornir · scrapli · Pydantic",
            border_style="cyan",
        )
    )

    # ---- Initialise (validate inventory + load creds + build Nornir) ----
    try:
        with console.status("[cyan]Validating inventory & initialising runtime..."):
            nr, validated_inv = init_engine(
                inventory_dir=args.inventory,
                num_workers=args.workers,
            )
    except FileNotFoundError as exc:
        err_console.print(f"Inventory error: {exc}")
        return 2
    except ValidationError as exc:
        err_console.print("Inventory validation failed:")
        err_console.print(str(exc))
        return 2
    except MissingCredentialsError as exc:
        err_console.print(f"Credential error: {exc}")
        return 2
    except ValueError as exc:
        err_console.print(f"Inventory integrity error: {exc}")
        return 2

    render_inventory(validated_inv)

    if args.validate_only:
        console.print("[bold green]✔ Inventory and credentials valid. Exiting.[/bold green]")
        return 0

    # ---- Execute ----
    target_desc = f"group '{args.group}'" if args.group else "all hosts"
    with console.status(
        f"[cyan]Executing [bold]'{args.command}'[/bold] against {target_desc} "
        f"({args.workers} workers)..."
    ):
        report = run_show_version(nr, command=args.command, group=args.group)

    render_report(report, verbose=args.verbose)

    return 0 if report.all_succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
