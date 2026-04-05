"""
core.models
===========

Strict Pydantic v2 data models for validating the Nornir YAML inventory
*before* it is handed to the orchestration runtime.

Nornir's SimpleInventory plugin is forgiving — it will happily load a host
with a malformed IP or an unsupported platform and only fail when a
connection is attempted. In a production NetDevOps pipeline we want those
failures at load-time, not at run-time against live gear.
"""

from __future__ import annotations

from enum import Enum
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class SupportedPlatform(str, Enum):
    """Scrapli-compatible platform identifiers supported by this engine."""

    CISCO_IOSXE = "cisco_iosxe"
    CISCO_NXOS = "cisco_nxos"
    CISCO_IOSXR = "cisco_iosxr"
    ARISTA_EOS = "arista_eos"
    JUNIPER_JUNOS = "juniper_junos"


# ---------------------------------------------------------------------------
# Connection / transport models
# ---------------------------------------------------------------------------


class ScrapliExtras(BaseModel):
    """Optional scrapli driver parameters passed through `extras`."""

    model_config = ConfigDict(extra="allow")

    transport: str = Field(default="asyncssh")
    auth_strict_key: bool = Field(default=False)
    timeout_socket: int | None = Field(default=None, ge=1)
    timeout_transport: int | None = Field(default=None, ge=1)
    timeout_ops: int | None = Field(default=None, ge=1)


class ScrapliConnectionOptions(BaseModel):
    """Per-connection scrapli configuration block."""

    model_config = ConfigDict(extra="forbid")

    platform: SupportedPlatform | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    extras: ScrapliExtras | None = None


class ConnectionOptions(BaseModel):
    """Map of connection plugin name -> options. Only scrapli is enforced."""

    model_config = ConfigDict(extra="allow")

    scrapli: ScrapliConnectionOptions | None = None


# ---------------------------------------------------------------------------
# Inventory node models
# ---------------------------------------------------------------------------


class InventoryDefaults(BaseModel):
    """Schema for inventory/defaults.yaml."""

    model_config = ConfigDict(extra="forbid")

    platform: SupportedPlatform | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    connection_options: ConnectionOptions | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class InventoryGroup(BaseModel):
    """Schema for a single entry in inventory/groups.yaml."""

    model_config = ConfigDict(extra="forbid")

    hostname: str | None = None
    platform: SupportedPlatform | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    groups: list[str] = Field(default_factory=list)
    connection_options: ConnectionOptions | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class InventoryHost(BaseModel):
    """
    Schema for a single entry in inventory/hosts.yaml.

    `hostname` is required and MUST be a valid IPv4 address — this engine
    targets IP-addressed network gear and deliberately rejects DNS names to
    avoid resolution ambiguity during change windows.
    """

    model_config = ConfigDict(extra="forbid")

    hostname: IPv4Address
    platform: SupportedPlatform | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None
    groups: list[str] = Field(default_factory=list)
    connection_options: ConnectionOptions | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("groups")
    @classmethod
    def _groups_non_empty_strings(cls, v: list[str]) -> list[str]:
        for g in v:
            if not isinstance(g, str) or not g.strip():
                raise ValueError("group names must be non-empty strings")
        return v


class Inventory(BaseModel):
    """
    Fully-validated, cross-referenced inventory.

    Beyond per-node schema checks, this model enforces referential integrity:
    every group a host references must exist in `groups`.
    """

    model_config = ConfigDict(extra="forbid")

    hosts: dict[str, InventoryHost]
    groups: dict[str, InventoryGroup] = Field(default_factory=dict)
    defaults: InventoryDefaults = Field(default_factory=InventoryDefaults)

    @field_validator("hosts")
    @classmethod
    def _at_least_one_host(cls, v: dict[str, InventoryHost]) -> dict[str, InventoryHost]:
        if not v:
            raise ValueError("inventory must contain at least one host")
        return v

    def check_group_references(self) -> None:
        """Raise ValueError if any host references an undefined group."""
        known = set(self.groups)
        for name, host in self.hosts.items():
            missing = set(host.groups) - known
            if missing:
                raise ValueError(
                    f"host '{name}' references undefined group(s): {sorted(missing)}"
                )

    def resolved_platform(self, host_name: str) -> SupportedPlatform:
        """
        Resolve the effective platform for a host using Nornir's precedence
        (host > groups (in order) > defaults). Raises if none is found.
        """
        host = self.hosts[host_name]
        if host.platform:
            return host.platform
        for g in host.groups:
            grp = self.groups.get(g)
            if grp and grp.platform:
                return grp.platform
        if self.defaults.platform:
            return self.defaults.platform
        raise ValueError(
            f"host '{host_name}' has no resolvable platform "
            f"(not set on host, any group, or defaults)"
        )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _read_yaml(path: Path) -> Any:
    if not path.is_file():
        raise FileNotFoundError(f"inventory file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_and_validate_inventory(inventory_dir: Path) -> Inventory:
    """
    Load hosts.yaml / groups.yaml / defaults.yaml from `inventory_dir`,
    validate each against the strict schema, verify cross-references, and
    return a fully-typed :class:`Inventory`.

    Raises
    ------
    FileNotFoundError
        If any of the three inventory files are missing.
    pydantic.ValidationError
        If any file violates the schema (bad IP, unsupported platform, ...).
    ValueError
        If referential integrity checks fail.
    """
    inventory_dir = inventory_dir.resolve()

    raw_hosts = _read_yaml(inventory_dir / "hosts.yaml")
    raw_groups = _read_yaml(inventory_dir / "groups.yaml")
    raw_defaults = _read_yaml(inventory_dir / "defaults.yaml")

    inv = Inventory(
        hosts={name: InventoryHost(**(cfg or {})) for name, cfg in raw_hosts.items()},
        groups={name: InventoryGroup(**(cfg or {})) for name, cfg in raw_groups.items()},
        defaults=InventoryDefaults(**raw_defaults),
    )

    inv.check_group_references()
    # Force platform resolution for every host so unsupported / missing
    # platforms fail now, not on first connection.
    for host_name in inv.hosts:
        inv.resolved_platform(host_name)

    return inv
