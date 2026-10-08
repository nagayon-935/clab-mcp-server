import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import server
import tool_results


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "CLAB_HOST", None)
    monkeypatch.setattr(server, "CLAB_API_URL", None)
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    monkeypatch.setattr(
        server,
        "_inspect_node_labels",
        Mock(side_effect=RuntimeError("label lookup unavailable")),
    )


def node(name, kind="linux", **kwargs):
    return {"name": name, "container": f"clab-x-{name}", "kind": kind, **kwargs}


@pytest.mark.parametrize("shape", ["list", "containers", "keyed"])
def test_list_labs_groups_inspect_results_and_preserves_host_paths(monkeypatch, shape):
    containers = [
        {
            "lab_name": "lab-with-dashes",
            "name": "clab-lab-with-dashes-node-one",
            "kind": "linux",
            "state": "running",
            "absLabPath": "/remote/lab.clab.yml",
        },
        {
            "lab_name": "lab-with-dashes",
            "name": "clab-lab-with-dashes-r2",
            "state": "exited",
        },
    ]
    raw = (
        containers
        if shape == "list"
        else {"containers": containers}
        if shape == "containers"
        else {
            "lab-with-dashes": [
                {k: v for k, v in c.items() if k != "lab_name"} for c in containers
            ]
        }
    )
    run = Mock(return_value=SimpleNamespace(stdout=json.dumps(raw)))
    monkeypatch.setattr(server, "_run_clab", run)
    result = server.list_labs()
    run.assert_called_once_with(["inspect", "--all", "--format", "json"], timeout=120)
    lab = result["data"]["labs"][0]
    assert lab["lab_name"] == "lab-with-dashes"
    assert lab["node_count"] == 2
    assert lab["state"] == "mixed"
    assert lab["nodes"][0]["name"] == "node-one"
    assert lab["topology_path"] == "/remote/lab.clab.yml"
    assert lab["topology_path_location"] == "execution_host"


@pytest.mark.parametrize("raw", [[], {}, {"lab": []}, {"containers": []}])
def test_list_labs_no_deployed_labs_is_success(monkeypatch, raw):
    monkeypatch.setattr(
        server, "_run_clab", lambda *a, **kw: SimpleNamespace(stdout=json.dumps(raw))
    )
    result = server.list_labs()
    assert result["status"] == "success"
    assert result["data"]["labs"] == []


def test_list_labs_does_not_guess_ambiguous_lab_name(monkeypatch):
    monkeypatch.setattr(
        server, "_inspect_containers", lambda: [{"name": "clab-lab-with-dashes-r1"}]
    )
    result = server.list_labs()
    assert result["status"] == "error"
    assert result["errors"][0]["code"] == "LAB_NAME_MISSING"


def test_list_labs_uses_api_backend(monkeypatch):
    import httpx

    monkeypatch.setattr(server, "CLAB_API_URL", "http://api-host:8080")
    get = Mock(
        return_value=SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"lab": [{"name": "clab-lab-r1", "state": "running"}]},
        )
    )
    monkeypatch.setattr(httpx, "get", get)
    result = server.list_labs()
    get.assert_called_once_with("http://api-host:8080/api/v1/labs", timeout=60.0)
    assert result["data"]["labs"][0]["execution_host"] == "api-host"


def test_list_topologies_keeps_duplicate_names_and_reports_bad_files(tmp_path):
    for name in ("a", "b"):
        (tmp_path / f"{name}.clab.yml").write_text(
            "name: x\ntopology: {nodes: {r1: {}}}"
        )
    (tmp_path / "bad.clab.yaml").write_text("name: [")
    result = server.list_topologies()
    assert result["status"] == "partial"
    assert result["counts"]["succeeded"] == 2
    assert result["counts"]["failed"] == 1
    assert all(t["location"] == "mcp_server" for t in result["data"]["topologies"])
    with pytest.raises(server.ToolInputError, match="複数"):
        server._find_topo_for_lab("x")


def test_node_names_use_exact_match_and_deduplicate():
    nodes = [node("r1"), node("r10"), node("r2")]
    assert server._select_nodes(nodes, node_names=["r1", "r1"]) == [nodes[0]]


@pytest.mark.parametrize(
    "kwargs, code",
    [
        ({"node_names": []}, "INVALID_ARGUMENT"),
        ({"node_names": ["r1", "typo"]}, "NODE_NOT_FOUND"),
        ({"node_names": ["r1"], "node_filter_regex": "r"}, "INVALID_ARGUMENT"),
        ({"node_filter_regex": "["}, "INVALID_ARGUMENT"),
        ({"max_output_chars": 0}, "INVALID_ARGUMENT"),
    ],
)
def test_invalid_selection_never_executes_commands(monkeypatch, kwargs, code):
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [node("r1")])
    execute = Mock()
    monkeypatch.setattr(server, "_run_nornir", execute)
    result = server.run_parallel_command("x", "interfaces", **kwargs)
    assert result["errors"][0]["code"] == code
    execute.assert_not_called()


def test_label_inheritance_and_exact_name_selection(tmp_path, monkeypatch):
    (tmp_path / "x.clab.yml").write_text("""
name: x
topology:
  defaults: {labels: {site: tokyo, role: spine}}
  kinds: {linux: {labels: {vendor: linux}}}
  groups: {leaves: {labels: {role: leaf}}}
  nodes:
    r1: {kind: linux, group: leaves, labels: {site: osaka}}
    r10: {kind: linux}
""")
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [node("r1"), node("r10")])
    selected, warnings = server._command_nodes(
        "x", ["r1"], None, {"role": "leaf", "site": "osaka"}
    )
    assert [n["name"] for n in selected] == ["r1"]
    assert selected[0]["labels"]["vendor"] == "linux"
    assert warnings


def test_inspect_labels_take_precedence_over_local_yaml(tmp_path, monkeypatch):
    (tmp_path / "x.clab.yml").write_text(
        "name: x\ntopology: {nodes: {r1: {labels: {role: leaf}}}}"
    )
    monkeypatch.setattr(
        server, "_inspect_nodes", lambda lab: [node("r1", labels={"role": "spine"})]
    )
    selected, warnings = server._command_nodes("x", None, None, {"role": "spine"})
    assert selected[0]["labels"] == {"role": "spine"}
    assert not warnings


def test_live_labels_take_precedence_over_local_yaml(tmp_path, monkeypatch):
    (tmp_path / "x.clab.yml").write_text(
        "name: x\ntopology: {nodes: {r1: {labels: {role: leaf}}}}"
    )
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [node("r1")])
    lookup = Mock(return_value={"clab-x-r1": {"role": "spine"}})
    monkeypatch.setattr(server, "_inspect_node_labels", lookup)
    selected, warnings = server._command_nodes("x", None, None, {"role": "spine"})
    assert selected[0]["labels"] == {"role": "spine"}
    assert not warnings
    lookup.assert_called_once()


def test_command_counts_include_failed_and_ineligible_nodes(monkeypatch):
    monkeypatch.setattr(
        server,
        "_inspect_nodes",
        lambda lab: [node("r1"), node("r2"), node("r3", "arista_ceos")],
    )

    def execute(container, command, timeout):
        if container.endswith("r2"):
            raise RuntimeError("Connection refused")
        return "healthy"

    monkeypatch.setattr(server, "_run_docker_exec", execute)
    result = server.run_parallel_command("x", "uptime")
    assert result["status"] == "partial"
    assert result["counts"] == {"total": 3, "succeeded": 1, "failed": 1, "skipped": 1}
    assert result["errors"][0]["node"] == "r2"
    assert result["errors"][0]["code"] == "UNREACHABLE"
    assert result["data"]["results"]["r3"]["reason"]
    assert result["next_steps"]


def test_output_paging_does_not_rerun_command(monkeypatch):
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [node("r1")])
    execute = Mock(return_value="あいうえおかきくけこ")
    monkeypatch.setattr(server, "_run_docker_exec", execute)
    result = server.run_node_command("x", "r1", "uptime", max_output_chars=4)
    entry = result["data"]["results"]["r1"]
    assert entry["output"] == "あいうえ"
    info = entry["output_info"]
    second = server.read_command_output(info["output_id"], info["next_offset"], 4)
    third = server.read_command_output(
        info["output_id"], second["data"]["next_offset"], 4
    )
    assert (
        entry["output"] + second["data"]["output"] + third["data"]["output"]
        == execute.return_value
    )
    assert third["data"]["next_offset"] is None
    execute.assert_called_once()


def test_diagnosis_separates_host_ssh_failure_from_dependent_checks(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", "remote")
    run = Mock(side_effect=RuntimeError("Permission denied (publickey)."))
    monkeypatch.setattr(server, "_run_argv", run)
    result = server.diagnose_environment("x", check_node_connections=True)
    assert run.call_count == 1
    checks = {c["name"]: c for c in result["data"]["checks"]}
    assert checks["host_ssh"]["error"]["code"] == "AUTHENTICATION_FAILED"
    assert (
        checks["clab_version"]["status"]
        == checks["docker_access"]["status"]
        == "skipped"
    )
    assert checks["lab_nodes"]["status"] == "skipped"


def test_diagnosis_checks_clab_and_docker_independently(monkeypatch):
    monkeypatch.setattr(
        server, "_run_clab", Mock(side_effect=RuntimeError("clab not found"))
    )
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout="26.0", stderr=""))
    monkeypatch.setattr(server, "_run_argv", run)
    monkeypatch.setattr(server, "_inspect_containers", lambda: [])
    result = server.diagnose_environment()
    checks = {c["name"]: c for c in result["data"]["checks"]}
    assert checks["clab_version"]["status"] == "error"
    assert checks["docker_access"]["status"] == "success"
    assert result["status"] == "partial"


def test_node_diagnosis_requires_explicit_opt_in(monkeypatch):
    monkeypatch.setattr(
        server, "_run_clab", lambda *a, **kw: SimpleNamespace(stdout="version")
    )
    monkeypatch.setattr(
        server,
        "_run_argv",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    monkeypatch.setattr(server, "_inspect_containers", lambda: [])
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [node("r1")])
    connect = Mock(return_value="")
    monkeypatch.setattr(server, "_run_docker_exec", connect)
    server.diagnose_environment("x")
    connect.assert_not_called()
    result = server.diagnose_environment("x", check_node_connections=True)
    connect.assert_called_once_with("clab-x-r1", "true", 15)
    assert result["status"] == "success"


def test_test_engine_accepts_exact_node_list(monkeypatch):
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [node("r1"), node("r10")])
    execute = Mock(return_value="ok")
    monkeypatch.setattr(server, "_run_docker_exec", execute)
    outcomes = server._run_test_case(
        "x", {"nodes": ["r1"], "command": "uptime", "assert": {"contains": "ok"}}
    )
    assert [o["node"] for o in outcomes] == ["r1"]
    assert outcomes[0]["passed"]
    execute.assert_called_once()


def test_output_store_rejects_expired_and_evicted_handles(monkeypatch):
    store = tool_results.OutputStore(ttl=10, max_entries=1)
    clock = Mock(return_value=0)
    monkeypatch.setattr(tool_results.time, "monotonic", clock)
    first = store.save("first")
    second = store.save("second")
    with pytest.raises(tool_results.ToolInputError):
        store.read(first, 0, 5)
    clock.return_value = 11
    with pytest.raises(tool_results.ToolInputError):
        store.read(second, 0, 5)


def test_mcp_exposes_structured_results_and_node_names(monkeypatch):
    monkeypatch.setattr(server, "_inspect_containers", lambda: [])
    tools = asyncio.run(server.mcp.list_tools())
    command = next(t for t in tools if t.name == "run_parallel_command")
    assert "node_names" in command.inputSchema["properties"]
    assert "node_labels" in command.inputSchema["properties"]
    assert command.outputSchema is not None
    assert "status" in command.outputSchema["required"]
    assert (
        next(
            t for t in tools if t.name == "diagnose_environment"
        ).annotations.readOnlyHint
        is True
    )
    result = asyncio.run(server.mcp.call_tool("list_labs", {}))
    # FastMCP supplies content blocks together with structuredContent.
    if isinstance(result, tuple):
        assert result[1]["status"] == "success"
    elif isinstance(result, dict):
        assert result["status"] == "success"
    else:
        assert json.loads(result[0].text)["status"] == "success"


@pytest.mark.parametrize(
    "name, args, flag",
    [
        ("deploy_lab", {"reconfigure": True}, "--reconfigure"),
        ("apply_lab", {"dry_run": True}, "--dry-run"),
        ("destroy_lab", {"cleanup": True}, "--cleanup"),
        ("redeploy_lab", {"cleanup": True}, "--cleanup"),
    ],
)
def test_lifecycle_tools_keep_flags_and_return_common_envelope(
    tmp_path, monkeypatch, name, args, flag
):
    topo = tmp_path / "x.clab.yml"
    topo.write_text("name: x")
    run = Mock(return_value=SimpleNamespace(stdout="done"))
    monkeypatch.setattr(server, "_run_clab", run)
    result = getattr(server, name)(str(topo), **args)
    assert flag in run.call_args.args[0]
    assert result["status"] == "success"
    assert result["data"]["output"] == "done"
    assert set(result) == {
        "tool",
        "status",
        "summary",
        "counts",
        "data",
        "warnings",
        "errors",
        "next_steps",
    }
    if name == "apply_lab":
        assert result["data"]["dry_run"] is True
        assert "未適用" in result["summary"]


def test_restart_empty_selection_cannot_restart_all_nodes(monkeypatch):
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [node("r1")])
    run = Mock()
    monkeypatch.setattr(server, "_run_clab", run)
    result = server.restart_lab_nodes("x", node_names=[])
    assert result["status"] == "error"
    run.assert_not_called()


def test_test_engine_reports_missing_management_ip_as_failure(monkeypatch):
    monkeypatch.setattr(
        server, "_inspect_nodes", lambda lab: [node("r1", "arista_ceos")]
    )
    result = server._run_test_case(
        "x", {"nodes": ["r1"], "command": "interfaces", "assert": {"contains": "up"}}
    )
    assert result[0]["node"] == "r1"
    assert result[0]["passed"] is False
    assert "管理IP" in result[0]["detail"]


def test_test_engine_empty_node_list_cannot_select_every_node(monkeypatch):
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [node("r1")])
    execute = Mock()
    monkeypatch.setattr(server, "_run_nornir", execute)
    result = server._run_test_case("x", {"nodes": [], "command": "uptime"})
    assert result[0]["passed"] is False
    execute.assert_not_called()


def test_structured_output_is_preserved_when_not_truncated():
    value = [{"interface": "eth1", "state": "up"}]
    output, info = tool_results.present_output(value, 1000)
    assert output == value
    assert info["format"] == "json"
    assert info["truncated"] is False


def test_oversized_output_does_not_exhaust_store(monkeypatch):
    save = Mock()
    monkeypatch.setattr(tool_results.OUTPUT_STORE, "save", save)
    output, info = tool_results.present_output("x" * 1_000_001, 10)
    assert len(output) == 10
    assert "output_id" not in info
    assert info["unavailable_reason"]
    save.assert_not_called()


def test_output_storage_failure_preserves_successful_execution(monkeypatch):
    monkeypatch.setattr(
        tool_results.OUTPUT_STORE,
        "save",
        Mock(side_effect=OSError("read-only temp directory")),
    )
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [node("r1")])
    monkeypatch.setattr(server, "_run_docker_exec", Mock(return_value="abcdef"))
    result = server.run_node_command("x", "r1", "uptime", max_output_chars=3)
    assert result["status"] == "success"
    assert result["data"]["results"]["r1"]["output_info"]["unavailable_reason"]
