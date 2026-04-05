# NetDevOps Engine

A production-ready, **async** network orchestration engine built on
[Nornir](https://nornir.readthedocs.io/), [scrapli](https://carlmontanari.github.io/scrapli/),
[Pydantic v2](https://docs.pydantic.dev/) and [Rich](https://rich.readthedocs.io/).

The engine strictly validates your device inventory *before* touching the
network, loads credentials securely from the environment, and fans out
commands concurrently over async SSH — rendering results in a clean,
colourised terminal UI.

---

## Features

- **Strict inventory validation** — Pydantic v2 models enforce valid IPv4
  management addresses, supported platforms, and group referential integrity
  at load time. Bad inventory fails fast, not mid-change-window.
- **Async transport** — scrapli's `asyncssh` transport drives true concurrent
  I/O against every target; the threaded Nornir runner manages the event
  loops per worker.
- **Secret-free YAML** — credentials never live in inventory files. They are
  injected at runtime from `NET_USER` / `NET_PASS` via `python-dotenv`.
- **Rich CLI** — inventory summary tables, per-host status, colourised
  success/failure breakdown, optional full raw-output panels.

---

## Project Layout

```
netdevops_engine/
├── core/
│   ├── __init__.py
│   ├── engine.py        # Nornir init, credential loading, async task runner
│   └── models.py        # Pydantic v2 inventory schema + loader
├── inventory/
│   ├── defaults.yaml
│   ├── groups.yaml
│   └── hosts.yaml
├── .env.example
├── main.py              # CLI entry point (argparse + rich)
├── requirements.txt
└── README.md
```

---

## Requirements

- Python **3.10+**
- Network reachability (SSH/22) to the devices in `inventory/hosts.yaml`

---

## Installation

```bash
# 1. Clone / enter the project
cd netdevops_engine

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
```

---

## Configuration

### 1. Credentials (`.env`)

Copy the template and fill in real device credentials:

```bash
cp .env.example .env
```

Then edit `.env`:

```dotenv
NET_USER=your_username
NET_PASS=your_password
```

> `.env` is read automatically at startup. Real environment variables
> (e.g. exported in your shell or injected by CI) always take precedence
> over the file. **Never commit `.env` to version control.**

### 2. Inventory

Edit the YAML files under `inventory/` to match your estate.

`inventory/hosts.yaml` — one entry per device. `hostname` **must** be a valid
IPv4 address:

```yaml
core-rtr-01:
  hostname: 192.0.2.11
  groups:
    - core
  data:
    site: hq
    role: core-router
```

`inventory/groups.yaml` — shared platform / connection settings:

```yaml
core:
  platform: cisco_iosxe
  connection_options:
    scrapli:
      platform: cisco_iosxe
      extras:
        transport: asyncssh
        auth_strict_key: false
```

Supported `platform` values: `cisco_iosxe`, `cisco_nxos`, `cisco_iosxr`,
`arista_eos`, `juniper_junos`.

---

## Running

All commands are run from inside the `netdevops_engine/` directory.

### Validate only (no device connections)

```bash
python main.py --validate-only
```

Confirms the inventory parses, every IP is valid, every platform is
supported, every referenced group exists, and credentials are loadable.

### Run `show version` against every host

```bash
python main.py
```

### Run a custom command against a single group, with full output

```bash
python main.py -c "show ip interface brief" -g core -v
```

### Full CLI reference

```text
usage: netdevops-engine [-h] [-c COMMAND] [-g GROUP] [-i INVENTORY]
                        [-w WORKERS] [--validate-only] [-v]

options:
  -h, --help            show this help message and exit
  -c, --command         Read-only CLI command to execute (default: 'show version')
  -g, --group           Restrict execution to hosts in this Nornir group
  -i, --inventory       Path to inventory directory
  -w, --workers         Number of concurrent workers (default: 10)
  --validate-only       Validate inventory and credentials, then exit
  -v, --verbose         Print full raw device output for every host
```

---

## Exit Codes

| Code | Meaning                                               |
|------|-------------------------------------------------------|
| `0`  | All targeted hosts succeeded                          |
| `1`  | One or more hosts failed during execution             |
| `2`  | Pre-flight failure (bad inventory, missing creds, …)  |

These make the engine safe to wire into CI/CD pipelines and pre-change
validation gates.

---

## How the Async Execution Works

Nornir's `threaded` runner spawns `-w` worker threads. Each worker picks up a
host and invokes `nornir_scrapli.tasks.send_command`. Because every host's
scrapli connection is configured with `transport: asyncssh`, scrapli spins up
an asyncio event loop inside the worker and drives the SSH session
non-blockingly — so you get concurrent device I/O without managing
`asyncio.gather()` yourself, while keeping Nornir's inventory, filtering and
result-aggregation ergonomics.

---

## Extending

- Add new device groups in `inventory/groups.yaml` — the Pydantic layer will
  reject any unsupported `platform` automatically.
- Add new orchestration tasks in `core/engine.py` following the
  `_task_show_version` pattern, then expose them via `main.py`.
- Tighten or relax validation in `core/models.py` (e.g. allow DNS hostnames
  by swapping `IPv4Address` for a custom validator).
