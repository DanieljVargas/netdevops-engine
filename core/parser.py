"""
core.parser
===========

Intelligence & Parsing layer — transforms raw CLI output into structured
Python dictionaries using scrapli's built-in TextFSM integration (which
leverages the ntc-templates library for vendor-specific template lookup).

Scrapli's ``Response.textfsm_parse_output()`` automatically resolves the
correct ntc-templates template based on the platform + command combination,
runs the TextFSM state machine, and returns a list of dicts.  This module
wraps that mechanism with:

* Graceful fallback — when no template exists for a command/platform pair,
  or when the template produces an empty match, the caller receives the raw
  text instead of an exception.
* Normalised per-host result containers that the CLI layer can consume
  without knowing whether parsing succeeded or not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from nornir.core import Nornir
from nornir.core.task import AggregatedResult, MultiResult, Result, Task
from nornir_scrapli.tasks import send_command

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result containers
# ---------------------------------------------------------------------------


@dataclass
class ParsedHostResult:
    """Per-host outcome carrying either structured data or raw fallback."""

    host: str
    hostname: str
    platform: str
    succeeded: bool
    parsed: bool = False
    structured_data: list[dict[str, Any]] = field(default_factory=list)
    raw_output: str = ""
    error: str = ""


@dataclass
class ParsedReport:
    """Aggregate result of a parsed multi-host run."""

    command: str
    results: list[ParsedHostResult] = field(default_factory=list)

    @property
    def ok_count(self) -> int:
        return sum(1 for r in self.results if r.succeeded)

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if not r.succeeded)

    @property
    def all_succeeded(self) -> bool:
        return self.failed_count == 0


# ---------------------------------------------------------------------------
# TextFSM parsing helper
# ---------------------------------------------------------------------------


def parse_scrapli_response(
    scrapli_result: Result,
) -> tuple[bool, list[dict[str, Any]], str]:
    """
    Attempt TextFSM parsing on a scrapli ``send_command`` Result.

    Returns
    -------
    (parsed, structured_data, raw_output)
        ``parsed`` is True when TextFSM produced at least one row.
        ``structured_data`` is the list-of-dicts on success, empty list on
        failure.  ``raw_output`` always carries the original CLI text.
    """
    raw = str(scrapli_result.result or "")

    # scrapli stores the underlying Response object on .scrapli_response
    scrapli_response = getattr(scrapli_result, "scrapli_response", None)

    if scrapli_response is None:
        log.warning("No scrapli_response attribute — cannot parse with TextFSM")
        return False, [], raw

    try:
        structured = scrapli_response.textfsm_parse_output()
    except Exception as exc:
        # Template missing / parse error — fall back gracefully.
        log.warning("TextFSM parsing failed: %s", exc)
        return False, [], raw

    if not structured:
        log.info("TextFSM returned empty result — falling back to raw output")
        return False, [], raw

    return True, structured, raw


# ---------------------------------------------------------------------------
# Nornir task
# ---------------------------------------------------------------------------


def _task_send_and_parse(task: Task, command: str) -> Result:
    """
    Execute ``command`` via scrapli, then attempt TextFSM parsing.

    The parsed payload (or raw fallback) is stored in ``result.result`` as a
    dict with keys ``parsed``, ``structured_data``, and ``raw_output`` so
    the caller can branch on success/failure without inspecting exceptions.
    """
    response = task.run(task=send_command, command=command)

    parsed, structured, raw = parse_scrapli_response(response)

    return Result(
        host=task.host,
        result={
            "parsed": parsed,
            "structured_data": structured,
            "raw_output": raw,
        },
        failed=response.failed,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _normalise_parsed_results(
    nr: Nornir,
    agg: AggregatedResult,
    command: str,
) -> ParsedReport:
    """Walk the Nornir AggregatedResult and build a :class:`ParsedReport`."""
    report = ParsedReport(command=command)

    for host_name, multi in agg.items():
        host_obj = nr.inventory.hosts[host_name]

        if multi.failed:
            exc = _first_exception(multi)
            report.results.append(
                ParsedHostResult(
                    host=host_name,
                    hostname=str(host_obj.hostname),
                    platform=str(host_obj.platform or ""),
                    succeeded=False,
                    error=str(exc) if exc else str(multi.result),
                )
            )
            continue

        # Our wrapper task stores the payload in the *first* Result of the
        # MultiResult (index 0 is the parent task, index 1+ are sub-tasks).
        payload = _extract_payload(multi)

        report.results.append(
            ParsedHostResult(
                host=host_name,
                hostname=str(host_obj.hostname),
                platform=str(host_obj.platform or ""),
                succeeded=True,
                parsed=payload.get("parsed", False),
                structured_data=payload.get("structured_data", []),
                raw_output=payload.get("raw_output", ""),
            )
        )

    return report


def _first_exception(multi: MultiResult) -> BaseException | None:
    for r in multi:
        if r.exception is not None:
            return r.exception
    return None


def _extract_payload(multi: MultiResult) -> dict:
    """Find the dict payload produced by ``_task_send_and_parse``."""
    for r in multi:
        if isinstance(r.result, dict) and "parsed" in r.result:
            return r.result
    return {"parsed": False, "structured_data": [], "raw_output": str(multi.result or "")}


def run_and_parse(
    nr: Nornir,
    command: str,
    group: str | None = None,
) -> ParsedReport:
    """
    Execute ``command`` asynchronously across all hosts (optionally filtered
    to ``group``), attempt TextFSM parsing per host, and return a structured
    :class:`ParsedReport`.

    If TextFSM cannot parse the output for a given host (missing template,
    empty match, or parse error) the per-host result falls back to raw text
    with ``parsed=False`` and a warning is logged — execution is **not**
    treated as a failure.
    """
    target = nr.filter(filter_func=lambda h: group in h.groups) if group else nr
    agg = target.run(task=_task_send_and_parse, command=command)
    return _normalise_parsed_results(target, agg, command)
