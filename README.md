**Languages:** English | [日本語](README.ja.md)

# clab-mcp-server

A hybrid MCP server that fuses Containerlab lifecycle management, legacy
operational script assets (command aliases, config snapshot/restore,
topology test engine), and multi-vendor parallel operations via
Nornir + Netmiko into a single server.

It keeps no fixed inventory file. On every tool call it queries
`clab inspect` for the current node state and builds a Nornir inventory
in memory before running tasks in parallel (a stateless design). The
entire implementation lives in the single file [server.py](server.py).

## Prerequisites

- Python 3.10+
- A host running [Containerlab](https://containerlab.dev/) (local or remote)
- [uv](https://docs.astral.sh/uv/) (recommended for running directly on a host)
- Docker (for running in a container)
- For packet capture: Wireshark on your local Mac, and `tshark` plus SSH
  reachability on the remote host

## Installation / Setup

### Option A: Run directly on a host (isolated with uv, recommended)

To avoid polluting the system Python environment, create a
project-local virtual environment (`.venv/`) with `uv` before running.

```bash
git clone <this-repo>
cd clab-mcp-server

# Resolve dependencies and install them into a project-local .venv/
uv sync

# Start the MCP server inside the isolated environment (smoke test)
uv run python server.py
```

`uv sync` reads `pyproject.toml` / `uv.lock` and installs dependencies
into `.venv/` without touching the system's site-packages. Regenerate
the lock file with `uv lock` whenever dependencies change.

### Option B: Run in Docker

```bash
docker build -t clab-mcp .
docker run -i --rm \
  -v ~/labs:/workspace \
  -v ~/.ssh:/home/mcp/.ssh:ro \
  -e CLAB_HOST=clab-host.example.com \
  clab-mcp
```

- Mount the host directory containing your topology YAML, `save/`, and
  `startup-configs/` at `/workspace`.
- Mount `~/.ssh` read-only if you use remote execution via `CLAB_HOST`
  or Netmiko key-based authentication.
- MCP communicates over stdio, so you must always pass `-i` (attach
  stdin) when starting the container.

## Registering with an MCP Client

Since the server starts over stdio, register `command`/`args` in your
client configuration.

**Via uv (host execution):**

```json
{
  "mcpServers": {
    "clab-hybrid": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/clab-mcp-server", "python", "server.py"],
      "env": {
        "CLAB_HOST": "clab-host.example.com"
      }
    }
  }
}
```

**Via Docker:**

```json
{
  "mcpServers": {
    "clab-hybrid": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "-v", "/path/to/labs:/workspace",
        "-v", "/Users/you/.ssh:/home/mcp/.ssh:ro",
        "-e", "CLAB_HOST=clab-host.example.com",
        "clab-mcp"
      ]
    }
  }
}
```

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `CLAB_BIN` | `clab` | Containerlab binary name |
| `CLAB_HOST` | unset | If set, runs `clab` on a remote host over SSH |
| `CLAB_SSH_USER` | unset | SSH user for the remote clab host |
| `CLAB_SUDO` | `0` | `1`/`true`/`yes` prefixes `clab` commands with `sudo` |
| `CLAB_API_URL` | unset | If set, switches deploy/inspect to use clab-api-server (httpx) |
| `NORNIR_WORKERS` | `20` | Max thread count for Nornir parallel execution |
| `NETMIKO_READ_TIMEOUT` | `60` | Netmiko command read_timeout (seconds) |
| `NETMIKO_SSH_CONFIG` | unset | Path to an ssh_config file passed to Netmiko (e.g. for ProxyJump) |
| `CLAB_USER_<KIND>` | see `KIND_DEFAULTS` | Overrides the username for a given kind (e.g. `CLAB_USER_ARISTA_CEOS`) |
| `CLAB_PASS_<KIND>` | see `KIND_DEFAULTS` | Overrides the password for a given kind |

`<KIND>` is the clab kind name upper-cased (e.g. `arista_ceos` →
`ARISTA_CEOS`). See `KIND_DEFAULTS` in `server.py` for the built-in
default credentials.

## Available Tools

| Tool | Summary |
|---|---|
| `deploy_lab(topo_yaml_path, reconfigure=False)` | Deploy a new lab from a topology YAML file (`reconfigure=True` adds `--reconfigure`, regenerating config artifacts) |
| `destroy_lab(topo_yaml_path, cleanup=False)` | Destroy a lab from a topology YAML file (`cleanup=True` adds `--cleanup`, deleting the lab directory entirely) |
| `inspect_lab_topology(lab_name)` | Get running nodes' mgmt IP, kind, and link info |
| `run_parallel_command(lab_name, command_or_alias, node_filter_regex=None)` | Run a command on all (or regex-filtered) nodes fully in parallel |
| `snapshot_and_save_configs(lab_name, mode="snapshot", save_dir="save", default_startup_dir="startup-configs")` | Collect configs from all nodes in parallel; save as a snapshot or write directly to startup-config |
| `restore_startup_configs(topo_path, snapshot_name="latest", save_dir="save")` | Restore a saved snapshot to each node's startup-config path |
| `run_topology_tests(test_file_or_dir)` | Recursively discover `test.yml` files and produce a PASS/FAIL report |
| `trigger_packet_capture(remote_host, container_name, interface_name)` | Stream a remote `tshark` capture into local Wireshark |

### Command Aliases for run_parallel_command

The following aliases are automatically translated into the
appropriate command for each node's kind (undefined kinds or strings
outside the alias table are run as-is, literally).

| Alias | Meaning |
|---|---|
| `bgp-summary` | Show BGP summary |
| `ip-route` | Show routing table |
| `interfaces` | Show interface status |

```text
run_parallel_command(lab_name="mylab", command_or_alias="bgp-summary")
run_parallel_command(lab_name="mylab", command_or_alias="show version", node_filter_regex="^r")
```

### test.yml Format for run_topology_tests

```yaml
lab: mylab
tests:
  - name: "BGP established on r1"
    nodes: "r1"              # node short name or regex
    command: "bgp-summary"   # alias or literal command
    assert:
      contains: "Established"   # or regex / exit_code
```

If `test_file_or_dir` points to a directory, all `test.yml` /
`test.yaml` files under it are discovered recursively and executed.

### Snapshot / Restore Directory Layout

```text
save/
  save-20260703-021500/
    r1.conf
    r2.conf
startup-configs/
  r1.conf   # default location for nodes with no startup-config defined in the topology
```

## Development

Run the test suite locally the same way CI does:

```bash
uv sync --all-groups   # installs pytest from the dev dependency group
uv run pytest -v
```

Tests live in `tests/` and cover the pure-logic parts of `server.py`
(command alias resolution, kind→platform mapping, inventory building,
topology YAML helpers, and the test-engine assertion logic) without
requiring a live Containerlab environment.

## CI/CD

GitHub Actions workflow: [.github/workflows/ci.yml](.github/workflows/ci.yml)

- **`test` job** — runs on every push to any branch. Installs
  dependencies with `uv sync --all-groups` and runs `uv run pytest -v`.
- **`publish-container` job** — runs only on push to `main`, and only
  if the `test` job succeeded. It reads the `version` field from
  `pyproject.toml`, builds the image from the [Dockerfile](Dockerfile),
  and pushes it to GitHub Container Registry as both
  `ghcr.io/<owner>/<repo>:<version>` and `ghcr.io/<owner>/<repo>:latest`.

To publish a new versioned image, bump `version` in `pyproject.toml`
before merging to `main` — the tag pushed to GHCR always matches that
value.

```bash
docker pull ghcr.io/<owner>/<repo>:<version>
```

## Troubleshooting

- **`clab` command not found**: Set `CLAB_HOST` to run on a remote host,
  or install Containerlab locally.
- **Cannot connect to a node**: When using `CLAB_HOST`, Netmiko connects
  directly to the mgmt IP over SSH, so mgmt-network reachability is
  required. If a jump host is needed, point `NETMIKO_SSH_CONFIG` at an
  ssh_config file with a ProxyJump entry.
- **`use_textfsm` output falls back to raw text**: This happens when no
  matching ntc-templates template exists for the command. `server.py`
  automatically falls back to raw text in that case, so this is
  expected behavior, not an error.
