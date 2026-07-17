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
- For packet capture: Wireshark on the machine running the MCP server
  (macOS, Windows, or Linux — resolved via `PATH` or the platform's
  default install location), and `tshark` plus SSH reachability on the
  remote host

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
| `apply_lab(topo_yaml_path, dry_run=False)` | Reconcile a running lab with the topology YAML (containerlab 0.77+ `apply`): deploys if the lab doesn't exist yet, otherwise only adds/removes the changed nodes/links instead of recreating everything. `dry_run=True` previews changes without applying them. Requires containerlab >= 0.77. |
| `destroy_lab(topo_yaml_path, cleanup=False)` | Destroy a lab from a topology YAML file (`cleanup=True` adds `--cleanup`, deleting the lab directory entirely) |
| `redeploy_lab(topo_yaml_path, cleanup=False)` | Destroy and redeploy a lab in one call (`clab redeploy`); `cleanup=True` adds `--cleanup` |
| `restart_lab_nodes(lab_name, node_names=None)` | Restart one, several, or all nodes in a running lab without recreating containers (`clab restart`, seamless dataplane). Omit `node_names` to restart every node. |
| `inspect_lab_topology(lab_name)` | Get running nodes' mgmt IP, kind, and link info |
| `run_parallel_command(lab_name, command_or_alias, node_filter_regex=None)` | Run a command on all (or regex-filtered) nodes fully in parallel |
| `run_node_command(lab_name, node_name, command=None)` | Run a command on exactly one node (the non-interactive equivalent of `scripts/clab-cli`), reporting the connection method and destination used. Useful for isolating per-node connectivity/auth failures that get lost in a parallel run. `command` defaults to the `interfaces` alias when omitted. |
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

### Connection Method by Kind

`run_parallel_command` and `run_node_command` dispatch on each node's `kind`:

- **`kind: linux`** (FRR/plain-Linux containers): these images typically don't
  run `sshd`, so the command is sent via `docker exec <container> sh -c
  "<command>"` instead — the same approach as `scripts/clab-exec-all` /
  `scripts/clab-cli`. When `CLAB_HOST` is set, `docker exec` runs over ssh on
  that remote host (not locally), since Docker itself only exists there.
- **All other kinds** (`cisco_xrd`, `arista_ceos`, `juniper_crpd`, etc.): sent
  via Netmiko/SSH to the node's mgmt IP, as described above.

If a `linux`-kind node's command keeps failing, use `run_node_command` to see
exactly which method and destination (`docker exec (<container>)` vs. `ssh
(<mgmt_ip>)`) was used and the raw error, instead of guessing from a
parallel-run summary.

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

**`exit_code` assertions only work on `kind: linux` nodes.** The test
engine appends `; echo __RC__=$?` to the command on `linux`-kind nodes
to capture the shell exit status; other kinds (Cisco/Arista/Juniper
etc.) have no equivalent mechanism, so an `exit_code` assertion against
them always fails with a "no `__RC__` marker" detail rather than being
silently treated as success.

### Snapshot / Restore Directory Layout

```text
save/
  save-20260703-021500/
    r1.conf
    r2.conf
startup-configs/
  r1.conf   # default location for nodes with no startup-config defined in the topology
```

`snapshot_and_save_configs` now also collects config from `kind: linux`
(FRR) nodes via `docker exec ... vtysh -c 'show running-config'`; plain
linux containers without `vtysh` (e.g. an L2-switch role container) are
skipped, same as kinds absent from `KIND_COMMAND`.

**Local filesystem note:** unlike `deploy_lab`/`destroy_lab`/etc., the
file I/O in `snapshot_and_save_configs` and `restore_startup_configs`
(`save_dir`, `default_startup_dir`, the topology file read) always
happens on the machine running the MCP server itself — it does **not**
go through `CLAB_HOST` over ssh. When running against a remote
containerlab host, make sure `save_dir`/the topology's directory is
reachable at the same local path (e.g. mount or sync the remote lab
directory) or these two tools won't line up with the actual lab.
Both tools now prepend a `⚠` warning line to their result summary
whenever `CLAB_HOST` is set, as a runtime reminder of this caveat.

## Development

Run the test suite locally the same way CI does:

```bash
uv sync --all-groups   # installs pytest, ruff, mypy from the dev dependency group
uv run ruff check .
uv run mypy server.py
uv run pytest -v
```

Tests live in `tests/` and cover the pure-logic parts of `server.py`
(command alias resolution, kind→platform mapping, inventory building,
topology YAML helpers, and the test-engine assertion logic) without
requiring a live Containerlab environment.

## CI/CD

GitHub Actions workflow: [.github/workflows/ci.yml](.github/workflows/ci.yml)

- **`test` job** — runs on every push to any branch. Installs
  dependencies with `uv sync --all-groups`, lints with `ruff check .`,
  type-checks with `mypy server.py`, then runs `uv run pytest -v`.
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
- **Cannot connect to a node**: First check whether it's a `kind: linux`
  node — those are accessed via `docker exec`, so make sure `docker` is
  runnable either locally (no `CLAB_HOST`) or on the `CLAB_HOST` machine
  (when set). For all other kinds, Netmiko connects directly to the mgmt
  IP over SSH when `CLAB_HOST` is set, so mgmt-network reachability is
  required; if a jump host is needed, point `NETMIKO_SSH_CONFIG` at an
  ssh_config file with a ProxyJump entry. Use `run_node_command` to see
  exactly which connection method, destination, and error apply to one
  specific node.
- **`use_textfsm` output falls back to raw text**: This happens when no
  matching ntc-templates template exists for the command. `server.py`
  automatically falls back to raw text in that case, so this is
  expected behavior, not an error.
