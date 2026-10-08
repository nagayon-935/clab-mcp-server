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
| `list_labs()` | Discover deployed labs, their state, node count, and host-side topology path |
| `list_topologies(search_dir=".")` | Discover local topology YAML files, including undeployed labs |
| `diagnose_environment(lab_name=None, check_node_connections=False)` | Check host SSH, clab, Docker, search location, and discovery independently; optionally check node connections and authentication |
| `deploy_lab(topo_yaml_path, reconfigure=False)` | Deploy a new lab from a topology YAML file (`reconfigure=True` adds `--reconfigure`, regenerating config artifacts) |
| `apply_lab(topo_yaml_path, dry_run=False)` | Reconcile a running lab with the topology YAML (containerlab 0.77+ `apply`): deploys if the lab doesn't exist yet, otherwise only adds/removes the changed nodes/links instead of recreating everything. `dry_run=True` previews changes without applying them. Requires containerlab >= 0.77. |
| `destroy_lab(topo_yaml_path, cleanup=False)` | Destroy a lab from a topology YAML file (`cleanup=True` adds `--cleanup`, deleting the lab directory entirely) |
| `redeploy_lab(topo_yaml_path, cleanup=False)` | Destroy and redeploy a lab in one call (`clab redeploy`); `cleanup=True` adds `--cleanup` |
| `restart_lab_nodes(lab_name, node_names=None)` | Restart one, several, or all nodes in a running lab without recreating containers (`clab restart`, seamless dataplane). Omit `node_names` to restart every node. |
| `inspect_lab_topology(lab_name)` | Get running nodes' mgmt IP, kind, and link info |
| `run_parallel_command(lab_name, command_or_alias, node_filter_regex=None, node_names=None, node_labels=None, max_output_chars=5000)` | Run parallel commands using exact node names, labels, or a regex |
| `run_node_command(lab_name, node_name, command=None, max_output_chars=5000)` | Run on one exact node; report connection method, destination, output, and failure cause. Default command: `interfaces` |
| `read_command_output(output_id, offset=0, max_output_chars=5000)` | Read another page of saved output without executing the command again |
| `snapshot_and_save_configs(lab_name, mode="snapshot", save_dir="save", default_startup_dir="startup-configs", node_names=None, node_labels=None)` | Collect configs from all or selected nodes; save as a snapshot or write startup-config files |
| `restore_startup_configs(topo_path, snapshot_name="latest", save_dir="save")` | Restore a saved snapshot to each node's startup-config path |
| `run_topology_tests(test_file_or_dir)` | Recursively discover `test.yml` files and produce a PASS/FAIL report |
| `trigger_packet_capture(remote_host, container_name, interface_name)` | Stream a remote `tshark` capture into local Wireshark |

### Discover and Operate a Lab

Ask the AI to "show deployed labs" or "check r1 and r2 in mylab". Example tool calls:

```text
list_labs()
list_topologies(search_dir="/path/to/labs")
inspect_lab_topology(lab_name="mylab")
run_parallel_command(lab_name="mylab", command_or_alias="interfaces", node_names=["r1", "r2"])
run_parallel_command(lab_name="mylab", command_or_alias="bgp-summary", node_labels={"role": "leaf"})
diagnose_environment()
diagnose_environment(lab_name="mylab", check_node_connections=True)
```

`node_names` matches exactly: `r1` does not select `r10`. An empty list or unknown
name fails before command execution. It cannot be combined with `node_filter_regex`.
`node_labels` requires all supplied key/value pairs to match and can be combined
with exact names. Missing inspect labels are fetched from Docker on the lab host.
If that lookup fails, they are supplemented from a matching local
topology's defaults, kinds, groups, and node definitions, with a warning. Keep that
YAML consistent with the deployed lab.

Diagnosis does not change configurations. Node checks require explicit opt-in:
Linux uses `docker exec ... true`; other kinds connect and authenticate through
Netmiko/SSH. Failed host SSH causes dependent checks to be marked unperformed.
`list_labs` reports paths on the lab host; `list_topologies` reports paths on the
MCP server. Those locations can differ when operating remotely.

### Common Results and Migration to 0.2

**Version 0.2 changes every tool's return value to a structured object.** Clients
that parsed prose or JSON strings should use `status` and `data`. Previous top-level
`nodes`, `links`, and command `results` fields now live under `data`.

```json
{
  "tool": "run_parallel_command",
  "status": "partial",
  "summary": "3台中1台で成功、1台で失敗、1台をスキップしました。",
  "counts": {"total": 3, "succeeded": 1, "failed": 1, "skipped": 1},
  "data": {"lab": "mylab", "results": {}},
  "warnings": [],
  "errors": [{"code": "AUTHENTICATION_FAILED", "message": "Authentication failed", "node": "r2", "next_step": "Check SSH keys and device credentials."}],
  "next_steps": ["Check SSH keys and device credentials."]
}
```

`status` is `success`, `partial`, `error`, or `skipped`. Counts refer to labs,
files, nodes, test outcomes, or diagnostic checks, depending on the tool.
Each entry in `data.results` includes its status, connection details, output,
failure cause, or skip reason. Available results are preserved when other nodes fail.

Command output defaults to 5000 characters per node. `max_output_chars` accepts
1–100000. Truncated output includes `output_info.truncated`, the character count,
`output_id`, and `next_offset`. Pass the ID and offset to `read_command_output`
to continue without rerunning the command. Truncated structured output is a JSON
text fragment. Truncation does not indicate execution failure.
Output is retained within the same process for up to one hour, 100 entries,
and one million characters per entry. Restart, expiration, or eviction invalidates
handles. Larger output is not retained and includes `unavailable_reason`.
Lab inventory is still discovered fresh on each operation.

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
  Linux nodes remain available even when they have no management IP.
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
    nodes: ["r1", "r2"]      # exact names; a string retains the legacy regex behavior
    command: "bgp-summary"   # alias or literal command
    assert:
      contains: "Established"   # or regex / exit_code
```

If `test_file_or_dir` points to a directory, all `test.yml` /
`test.yaml` files under it are discovered recursively and executed.

**`exit_code` assertions only work on `kind: linux` nodes.** Commands run
in a child shell so that `exit` and trailing comments cannot prevent
exit-status collection. Other kinds (Cisco/Arista/Juniper etc.) report
an unsupported-assertion failure before executing the command.
Invalid test files and missing lab names count as failures in the summary.

### Topology YAML Auto-Discovery

`inspect_lab_topology(lab_name)` (for link info) and
`snapshot_and_save_configs(lab_name, mode="startup")` don't take a
topology path directly — they recursively search the current working
directory for `*.clab.yml` / `*.clab.yaml` files whose `name:` field
matches `lab_name`. If no file's `name:` matches, they report it
explicitly (an empty `links` list with a warning, or an error for
`mode="startup"`) rather than guessing and falling back to an unrelated
topology file — run the MCP server from a directory containing (or
above) the relevant topology YAML.
Multiple matching files produce a warning for link discovery and an error for
startup saves. Use `list_topologies` to inspect the candidates.

### Snapshot / Restore Directory Layout

```text
save/
  save-20260703-021500-123456-abcdefgh/
    r1.conf
    r2.conf
startup-configs/
  r1.conf   # default location for nodes with no startup-config defined in the topology
```

`snapshot_and_save_configs` now also collects config from `kind: linux`
(FRR) nodes via `docker exec ... vtysh -c 'show running-config'`; plain
linux containers without `vtysh` (e.g. an L2-switch role container) are
skipped, same as kinds absent from `KIND_COMMAND`.
Connection failures and timeouts are reported as errors. Snapshot directories
include microseconds and a unique suffix to keep concurrent saves separate.
Existing `save-<timestamp>` directories can still be restored. Both explicit
snapshot names and `latest` must resolve to a directory inside `save_dir`;
symlinks pointing outside it are rejected.

**Local filesystem note:** unlike `deploy_lab`/`destroy_lab`/etc., the
file I/O in `snapshot_and_save_configs` and `restore_startup_configs`
(`save_dir`, `default_startup_dir`, the topology file read) always
happens on the machine running the MCP server itself — it does **not**
go through `CLAB_HOST` over ssh. When running against a remote
containerlab host, make sure `save_dir`/the topology's directory is
reachable at the same local path (e.g. mount or sync the remote lab
directory) or these two tools won't line up with the actual lab.
Both tools include this reminder in `warnings` whenever `CLAB_HOST` is set.

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
- **SSH to `CLAB_HOST` hangs or fails outright**: because the MCP server
  runs non-interactively, all ssh calls use `BatchMode=yes` — they never
  prompt for a host key or a password, they just fail fast instead. Make
  sure the `CLAB_HOST` key is already in `known_hosts` (e.g. connect
  once manually, or `ssh-keyscan`) and that key-based auth is set up
  before registering the server.
- **`CLAB_SUDO=1` fails with a sudo error instead of running**: `sudo`
  is invoked as `sudo -n` (non-interactive) for the same reason — if the
  remote user needs a password for `sudo`, configure passwordless sudo
  (`NOPASSWD`) for the relevant commands on `CLAB_HOST` instead.
- **Long-running remote commands fail with rc=124 / "timeout"**: when
  `CLAB_HOST` is set, remote commands are wrapped in coreutils `timeout`
  so a stalled `clab`/`docker exec` process on the remote host can't
  turn into an orphaned/zombie process. If a legitimately slow operation
  needs more time, it's using the timeout already passed to that tool
  (e.g. `deploy_lab`/`destroy_lab` default to 600s) — there's currently
  no per-call override.
