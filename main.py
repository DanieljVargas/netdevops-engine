#!/usr/bin/env python3
"""
NetDevOps Engine — CLI entry point.

Usage examples
--------------
    python main.py run                         # 'show version' on all hosts
    python main.py run -c "show ip int br"     # custom read-only command
    python main.py run -g core -v              # target the 'core' group
    python main.py parse-interfaces            # parsed 'show ip int brief', down only
    python main.py parse-interfaces -g core    # same, filtered to core group
    python main.py --validate-only             # just validate inventory + env
"""

from __future__ import annotations

import argparse
from pathlib import Path

from nornir.core import Nornir

from pydantic import ValidationError
from rich.console import Console
from rich.panel import Panel
from rich.rule import Rule
from rich.syntax import Syntax
from rich.table import Table
from rich.traceback import install as install_rich_traceback

# Importamos la tarea nativa de Scrapli correctamente
from nornir_scrapli.tasks import send_command

from core.engine import (
    DEFAULT_INVENTORY_DIR,
    ExecutionReport,
    MissingCredentialsError,
    ParsedReport,
    init_engine,
    run_and_parse,
    run_show_version,
    export_structured_data,
)
from core.models import Inventory

console = Console()
err_console = Console(stderr=True, style="bold red")
install_rich_traceback(show_locals=False, suppress=[])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Attach flags shared by every subcommand."""
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
        "-v",
        "--verbose",
        action="store_true",
        help="Print full raw device output for every host.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netdevops-engine",
        description="Async network orchestration engine (Nornir + scrapli).",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate inventory and credentials, then exit without "
             "connecting to devices.",
    )

    subs = parser.add_subparsers(dest="action")

    # ---- run: raw command execution (original behaviour) ----
    run_p = subs.add_parser("run", help="Execute a read-only CLI command.")
    run_p.add_argument(
        "-c",
        "--command",
        default="show version",
        help="CLI command to execute (default: 'show version').",
    )
    _add_common_args(run_p)

    # ---- parse-interfaces: parsed 'show ip interface brief' ----
    pi_p = subs.add_parser(
        "parse-interfaces",
        help="Run 'show ip interface brief', parse with TextFSM, "
             "and display interfaces that are down.",
    )
    _add_common_args(pi_p)

    # --- NUEVO COMANDO: AUDIT (Extracción Inteligente y Exportación) ---
    audit_parser = subs.add_parser(
        "audit", 
        help="Ejecuta comando, parsea con TextFSM y exporta a CSV o JSON"
    )
    audit_parser.add_argument("-c", "--command", required=True, help="Comando a parsear (ej. 'show inventory')")
    audit_parser.add_argument("--csv", help="Exportar a CSV (escribe el nombre del archivo sin el .csv)")
    audit_parser.add_argument("--json", help="Exportar a JSON (escribe el nombre del archivo sin el .json)")
    _add_common_args(audit_parser)

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


def render_parsed_interfaces(report: ParsedReport, verbose: bool) -> None:
    """
    Render a TextFSM-parsed ``show ip interface brief`` report, showing
    **only** interfaces whose status contains 'down' or 'administratively down'.

    Falls back to raw output per host when TextFSM could not parse.
    """
    console.print(Rule(f"[bold]Parsed Results — [cyan]{report.command}[/cyan]"))

    DOWN_KEYWORDS = {"down", "administratively down"}

    for hr in report.results:
        title = f"{hr.host} ({hr.hostname})"

        if not hr.succeeded:
            console.print(Panel(
                f"[red]{hr.error or 'unknown error'}[/red]",
                title=title,
                border_style="red",
            ))
            continue

        if not hr.parsed:
            console.print(
                f"  [yellow]⚠ TextFSM could not parse output for "
                f"[bold]{hr.host}[/bold] — falling back to raw text[/yellow]"
            )
            if verbose:
                console.print(Panel(
                    Syntax(hr.raw_output or "(empty)", "text",
                           theme="ansi_dark", word_wrap=True),
                    title=title,
                    border_style="yellow",
                ))
            continue

        # Filter: keep only rows where status or protocol is down.
        down_rows: list[dict] = []
        for row in hr.structured_data:
            status = str(row.get("status", "")).strip().lower()
            proto = str(row.get("proto", "")).strip().lower()
            if status in DOWN_KEYWORDS or proto in DOWN_KEYWORDS:
                down_rows.append(row)

        if not down_rows:
            console.print(
                f"  [green]✔ {hr.host}:[/green] all interfaces are up"
            )
            if verbose:
                console.print(
                    f"    ({len(hr.structured_data)} interface(s) parsed, "
                    f"none down)"
                )
            continue

        table = Table(
            title=title,
            show_lines=False,
            header_style="bold red",
            border_style="red",
        )
        table.add_column("Interface", style="bold")
        table.add_column("IP Address")
        table.add_column("Status")
        table.add_column("Protocol")

        for row in down_rows:
            intf = row.get("intf", row.get("interface", ""))
            ipaddr = row.get("ipaddr", row.get("ip_address", ""))
            status = row.get("status", "")
            proto = row.get("proto", row.get("protocol", ""))

            st_style = "red" if "admin" in status.lower() else "yellow"
            pr_style = "red" if "down" in proto.lower() else "green"

            table.add_row(
                str(intf),
                str(ipaddr) or "unassigned",
                f"[{st_style}]{status}[/{st_style}]",
                f"[{pr_style}]{proto}[/{pr_style}]",
            )

        console.print(table)

    # Summary footer
    ok_style = "bold green" if report.all_succeeded else "bold yellow"
    console.print(
        f"\n[{ok_style}]Succeeded: {report.ok_count}[/{ok_style}]   "
        f"[bold red]Failed: {report.failed_count}[/bold red]   "
        f"Total: {len(report.results)}\n"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _init_or_fail(args: argparse.Namespace) -> tuple[Nornir, Inventory] | int:
    """Shared init: validate inventory, load creds, build Nornir."""
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
    return nr, validated_inv


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    console.print(
        Panel.fit(
            "[bold cyan]NetDevOps Engine[/bold cyan]\n"
            "Async network orchestration — Nornir · scrapli · Pydantic · TextFSM",
            border_style="cyan",
        )
    )

    # If no subcommand and no --validate-only, show help.
    if not args.action and not args.validate_only:
        parser.print_help()
        return 0

    # ---- Initialise ----
    if not hasattr(args, "inventory"):
        args.inventory = DEFAULT_INVENTORY_DIR
    if not hasattr(args, "workers"):
        args.workers = 10
    if not hasattr(args, "group"):
        args.group = None
    if not hasattr(args, "verbose"):
        args.verbose = False

    result = _init_or_fail(args)
    if isinstance(result, int):
        return result
    nr, validated_inv = result

    render_inventory(validated_inv)

    if args.validate_only:
        console.print("[bold green]✔ Inventory and credentials valid. Exiting.[/bold green]")
        return 0

    # ---- Dispatch ----
    target_desc = f"group '{args.group}'" if args.group else "all hosts"

    if args.action == "run":
        with console.status(
            f"[cyan]Executing [bold]'{args.command}'[/bold] against {target_desc} "
            f"({args.workers} workers)..."
        ):
            report = run_show_version(nr, command=args.command, group=args.group)
        render_report(report, verbose=args.verbose)
        return 0 if report.all_succeeded else 1
    
    elif args.action == "audit":
        console.print(f"\n[bold blue]Auditoría Estructurada:[/bold blue] {args.command}")
        
        target = nr.filter(filter_func=lambda h: args.group in h.groups) if args.group else nr
        
        with console.status(f"[cyan]Ejecutando y parseando '{args.command}' en {target_desc}...[/cyan]"):
            results = target.run(task=send_command, command=args.command)
        
        exported = False
        if args.csv:
            file_path = export_structured_data(results, filename=args.csv, file_format="csv")
            if file_path:
                console.print(f"[bold green]✔ Datos exportados a: {file_path}[/bold green]")
                exported = True
            else:
                console.print("[bold red]✖ No se pudo parsear el comando para CSV.[/bold red]")
                
        if args.json:
            file_path = export_structured_data(results, filename=args.json, file_format="json")
            if file_path:
                console.print(f"[bold green]✔ Datos exportados a: {file_path}[/bold green]")
                exported = True
            else:
                console.print("[bold red]✖ No se pudo parsear el comando para JSON.[/bold red]")
                
        if not exported and (args.csv or args.json):
            console.print("[yellow]Asegúrate de que existe un template de TextFSM para este comando.[/yellow]")
        elif not args.csv and not args.json:
            console.print("[yellow]⚠ Comando ejecutado, pero no especificaste --csv o --json para exportar.[/yellow]")
            
        return 0

    elif args.action == "parse-interfaces":
        cmd = "show ip interface brief"
        with console.status(
            f"[cyan]Executing + parsing [bold]'{cmd}'[/bold] against {target_desc} "
            f"({args.workers} workers)..."
        ):
            parsed = run_and_parse(nr, command=cmd, group=args.group)
        render_parsed_interfaces(parsed, verbose=args.verbose)
        return 0 if parsed.all_succeeded else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())