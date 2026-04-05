# NetDevOps Engine

A production-ready, **async** network orchestration engine built on
[Nornir](https://nornir.readthedocs.io/), [scrapli](https://carlmontanari.github.io/scrapli/),
[Pydantic v2](https://docs.pydantic.dev/), [TextFSM (ntc-templates)](https://github.com/networktocode/ntc-templates) and [Rich](https://rich.readthedocs.io/).

The engine strictly validates your device inventory _before_ touching the
network, loads credentials securely from the environment, fans out
commands concurrently over SSH, and intelligently parses raw CLI output into structured Python data — rendering results in a clean, colourised terminal UI.

---

## Features

- **Strict Inventory Validation** — Pydantic v2 models enforce valid IPv4
  management addresses, supported platforms, and group referential integrity
  at load time. Bad inventory fails fast, not mid-change-window.
- **Intelligent Parsing (TextFSM)** — Automatically converts raw CLI text into structured data (JSON/Dictionaries) using `ntc-templates`. If a template fails, it degrades gracefully to raw text.
- **Robust Transport** — Uses `paramiko` via Scrapli for maximum compatibility across Windows, Linux, and WSL environments while maintaining Nornir's threaded concurrency.
- **Secret-free YAML** — Credentials never live in inventory files. They are
  injected at runtime from `NET_USER` / `NET_PASS` via `python-dotenv`.
- **Rich CLI Subcommands** — Clean interface for validation, raw execution, and specific parsed audits (e.g., finding down interfaces).

---

## Project Layout

```text
netdevops_engine/
├── core/
│   ├── __init__.py
│   ├── engine.py        # Nornir init, credential loading, async task runner
│   ├── models.py        # Pydantic v2 inventory schema + loader
│   └── parser.py        # TextFSM intelligence layer for CLI parsing
├── inventory/
│   ├── defaults.yaml
│   ├── groups.yaml
│   └── hosts.yaml
├── .env.example
├── main.py              # CLI entry point with subcommands
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
git clone <your-repo-url>
cd netdevops_engine

# 2. Create and activate a virtual environment (Linux/WSL recommended)
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install --upgrade pip
pip install -r requirements.txt
pip install scrapli[paramiko]
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
      extras:
        transport: paramiko # Highly recommended for cross-OS compatibility
        auth_strict_key: false
```

Supported `platform` values: `cisco_iosxe`, `cisco_nxos`, `cisco_iosxr`,
`arista_eos`, `juniper_junos`.

---

## Running

The CLI uses a subcommand architecture. All commands are run from inside the `netdevops_engine/` directory.

### 1. Validate only (no device connections)

Confirms the inventory parses, every IP is valid, every referenced group exists, and credentials are loadable.

```bash
python main.py validate
# or
python main.py --validate-only
```

### 2. Run a raw command (e.g., `show version`)

Executes a read-only command concurrently and returns raw CLI output.

```bash
python main.py run -c "show version"
python main.py run -c "show ip route" -g core -v
```

### 3. Intelligent Audit (TextFSM Parsing)

Runs `show ip interface brief`, parses the output automatically, and renders a filtered table showing **ONLY** interfaces that are physically or administratively down.

```bash
python main.py parse-interfaces
python main.py parse-interfaces -g core
```

---

## Exit Codes

| Code | Meaning                                              |
| ---- | ---------------------------------------------------- |
| `0`  | All targeted hosts succeeded                         |
| `1`  | One or more hosts failed during execution            |
| `2`  | Pre-flight failure (bad inventory, missing creds, …) |

These make the engine safe to wire into CI/CD pipelines and pre-change
validation gates.

---

## Extending

- **New Devices:** Add new device groups in `inventory/groups.yaml` — the Pydantic layer will reject any unsupported `platform` automatically.
- **New Parsed Audits:** Create a new subcommand in `main.py` that leverages `run_and_parse()` from `core.engine` to build custom logic (e.g., parsing BGP neighbors or OSPF states).
- **Validation Rules:** Tighten or relax validation in `core/models.py` (e.g. allow DNS hostnames by swapping `IPv4Address` for a custom validator).

## Author

Daniel Vargas — MikroTik Certified Network Engineer (MTCINE)  
[Upwork Profile](https://www.upwork.com/freelancers/~01c1996b74e4505213?mp_source=share)
[LinkedIn](www.linkedin.com/in/daniel-vargas-avila)
