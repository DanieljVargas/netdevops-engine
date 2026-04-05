"""
core.engine
===========

Nornir runtime initialisation and async task execution via scrapli.

Responsibilities
----------------
1. Load credentials from the environment (`.env` via python-dotenv).
2. Validate the YAML inventory with the strict Pydantic models.
3. Build a Nornir object wired to the local SimpleInventory files and the
   threaded runner (scrapli's async transport runs an event-loop per worker).
4. Expose :func:`run_show_version` — an async `show version` against all
   hosts, returning a structured result set.
5. Re-export :func:`run_and_parse` from :mod:`core.parser` so callers can
   import all orchestration functions from a single module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv
from nornir import InitNornir
from nornir.core import Nornir
from nornir.core.task import AggregatedResult, Result, Task
from nornir_scrapli.tasks import send_command

from core.models import Inventory, load_and_validate_inventory
from core.parser import ParsedHostResult, ParsedReport, run_and_parse

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PACKAGE_ROOT: Path = Path(__file__).resolve().parent.parent
DEFAULT_INVENTORY_DIR: Path = PACKAGE_ROOT / "inventory"
DEFAULT_DOTENV: Path = PACKAGE_ROOT / ".env"


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class MissingCredentialsError(RuntimeError):
    """Raised when NET_USER / NET_PASS are not available in the environment."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


def load_credentials(dotenv_path: Path | None = None) -> Credentials:
    """
    Load NET_USER / NET_PASS from the environment, after sourcing a local
    `.env` file if one exists. Real environment variables always win over
    values in the file (``override=False``).
    """
    env_file = dotenv_path if dotenv_path is not None else DEFAULT_DOTENV
    if env_file.is_file():
        load_dotenv(dotenv_path=env_file, override=False)
    else:
        # Fall back to auto-discovery (cwd upwards) so running from a
        # different directory still works.
        load_dotenv(override=False)

    user = os.getenv("NET_USER")
    password = os.getenv("NET_PASS")
    if not user or not password:
        raise MissingCredentialsError(
            "NET_USER and NET_PASS must be set — copy .env.example to .env "
            "and populate it, or export them in your shell."
        )
    return Credentials(username=user, password=password)


# ---------------------------------------------------------------------------
# Nornir initialisation
# ---------------------------------------------------------------------------


def init_engine(
    inventory_dir: Path | None = None,
    num_workers: int = 10,
    dotenv_path: Path | None = None,
) -> tuple[Nornir, Inventory]:
    """
    Validate inventory, build the Nornir runtime, and inject credentials.

    Returns
    -------
    (Nornir, Inventory)
        The live Nornir object plus the validated Pydantic view of the
        inventory (useful for rich rendering / reporting in the CLI layer).
    """
    inv_dir = (inventory_dir or DEFAULT_INVENTORY_DIR).resolve()

    # 1. Strict validation — fail fast on bad IPs / platforms / refs.
    validated = load_and_validate_inventory(inv_dir)

    # 2. Credentials from env.
    creds = load_credentials(dotenv_path=dotenv_path)

    # 3. Nornir wired to the local YAML files.
    nr = InitNornir(
        runner={
            "plugin": "threaded",
            "options": {"num_workers": num_workers},
        },
        inventory={
            "plugin": "SimpleInventory",
            "options": {
                "host_file": str(inv_dir / "hosts.yaml"),
                "group_file": str(inv_dir / "groups.yaml"),
                "defaults_file": str(inv_dir / "defaults.yaml"),
            },
        },
        logging={"enabled": False},
    )

    # 4. Inject credentials at the defaults level so every host inherits them
    #    without secrets ever touching the YAML on disk.
    nr.inventory.defaults.username = creds.username
    nr.inventory.defaults.password = creds.password

    return nr, validated


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _task_show_version(task: Task, command: str = "show version") -> Result:
    """
    Nornir task wrapper around scrapli's async `send_command`.

    scrapli is configured with the ``asyncssh`` transport (see
    inventory/groups.yaml), so each worker thread spins up an asyncio event
    loop and drives the device connection concurrently — giving us true
    async I/O fan-out without the caller needing to manage the loop.
    """
    response = task.run(task=send_command, command=command)
    return Result(
        host=task.host,
        result=response.result,
        failed=response.failed,
    )


@dataclass
class HostExecutionResult:
    """Normalised per-host outcome for consumption by the CLI layer."""

    host: str
    hostname: str
    platform: str
    succeeded: bool
    output: str = ""
    error: str = ""
    changed: bool = False
    elapsed: float | None = None


@dataclass
class ExecutionReport:
    """Aggregate result of a multi-host run."""

    command: str
    results: list[HostExecutionResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.succeeded)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.succeeded)

    @property
    def all_succeeded(self) -> bool:
        return self.failed_count == 0


def _normalise_results(
    nr: Nornir, agg: AggregatedResult, command: str
) -> ExecutionReport:
    report = ExecutionReport(command=command)
    for host_name, multi in agg.items():
        host_obj = nr.inventory.hosts[host_name]
        failed = multi.failed
        # First sub-result is our wrapper; dig for the scrapli payload / exc.
        output = ""
        error = ""
        if failed:
            exc = None
            for r in multi:
                if r.exception is not None:
                    exc = r.exception
                    break
            error = str(exc) if exc is not None else str(multi.result)
        else:
            # The scrapli send_command result carries the raw CLI text.
            for r in multi:
                if r.name == "send_command" and r.result:
                    output = str(r.result)
                    break
            if not output:
                output = str(multi.result or "")

        report.results.append(
            HostExecutionResult(
                host=host_name,
                hostname=str(host_obj.hostname),
                platform=str(host_obj.platform or ""),
                succeeded=not failed,
                output=output,
                error=error,
                changed=multi.changed,
            )
        )
    return report


def run_show_version(
    nr: Nornir,
    command: str = "show version",
    group: str | None = None,
) -> ExecutionReport:
    """
    Execute ``show version`` (or any read-only command) asynchronously
    against all hosts — optionally filtered to a single group — and return
    a structured :class:`ExecutionReport`.
    """
    target = nr.filter(filter_func=lambda h: group in h.groups) if group else nr
    agg = target.run(task=_task_show_version, command=command)
    return _normalise_results(target, agg, command)
