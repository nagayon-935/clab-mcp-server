import subprocess

import pytest

import server


def test_evaluate_assertion_contains_pass_and_fail():
    ok, detail = server._evaluate_assertion("BGP Established peer", {"contains": "Established"})
    assert ok is True
    assert "OK" in detail

    ok, detail = server._evaluate_assertion("BGP Idle", {"contains": "Established"})
    assert ok is False
    assert "NG" in detail


def test_evaluate_assertion_regex():
    ok, _ = server._evaluate_assertion("uptime 3 days", {"regex": r"uptime \d+ days"})
    assert ok is True

    ok, _ = server._evaluate_assertion("uptime unknown", {"regex": r"uptime \d+ days"})
    assert ok is False


def test_evaluate_assertion_exit_code():
    ok, _ = server._evaluate_assertion("some output\n__RC__=0", {"exit_code": 0})
    assert ok is True

    ok, _ = server._evaluate_assertion("some output\n__RC__=1", {"exit_code": 0})
    assert ok is False


def test_evaluate_assertion_exit_code_fails_when_marker_missing():
    """__RC__ マーカーは linux kind のみ付与される。NOS ノード等でマーカーが
    無い出力を actual=0 とみなして誤 PASS させないこと（マーカー無し=FAIL）。"""
    ok, detail = server._evaluate_assertion(
        "% Invalid input detected", {"exit_code": 0}
    )
    assert ok is False
    assert "__RC__" in detail


def test_evaluate_assertion_no_condition_fails_with_message():
    ok, detail = server._evaluate_assertion("anything", {})
    assert ok is False
    assert "アサーション条件" in detail


def test_discover_test_files_single_file(tmp_path):
    test_file = tmp_path / "test.yml"
    test_file.write_text("lab: mylab\ntests: []\n")

    found = server._discover_test_files(str(test_file))

    assert found == [str(test_file)]


def test_discover_test_files_recurses_into_directories(tmp_path):
    nested = tmp_path / "suite_a"
    nested.mkdir()
    (nested / "test.yml").write_text("lab: mylab\ntests: []\n")
    (tmp_path / "test.yaml").write_text("lab: mylab\ntests: []\n")
    (tmp_path / "not-a-test.txt").write_text("ignore me")

    found = server._discover_test_files(str(tmp_path))

    assert len(found) == 2
    assert all(f.endswith(("test.yml", "test.yaml")) for f in found)


def test_discover_test_files_raises_for_missing_path():
    with pytest.raises(RuntimeError):
        server._discover_test_files("/no/such/path/here")


def test_load_test_cases_reads_lab_and_tests(tmp_path):
    test_file = tmp_path / "test.yml"
    test_file.write_text(
        """
lab: mylab
tests:
  - name: "BGP established on r1"
    nodes: "r1"
    command: "bgp-summary"
    assert:
      contains: "Established"
"""
    )

    lab_name, cases = server._load_test_cases(str(test_file))

    assert lab_name == "mylab"
    assert len(cases) == 1
    assert cases[0]["name"] == "BGP established on r1"
    assert cases[0]["assert"] == {"contains": "Established"}


@pytest.mark.parametrize(
    "command, expected",
    [
        ("exit 7", 7),
        ("false # trailing comment", 1),
        ("printf 'no newline'", 0),
        ("printf '__RC__=0\\n'; exit 7", 7),
        ("printf 'quote: \" and dollar: $'", 0),
    ],
)
def test_exit_code_uses_shell_status_even_with_exit_comments_or_markers(
    monkeypatch, command, expected
):
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [
        {"name": "r1", "container": "clab-x-r1", "kind": "linux"}
    ])

    def fake_docker_exec(container, wrapped_command, timeout):
        proc = subprocess.run(
            ["sh", "-c", wrapped_command], capture_output=True, text=True, check=True
        )
        return proc.stdout

    monkeypatch.setattr(server, "_run_docker_exec", fake_docker_exec)
    outcomes = server._run_test_case("x", {
        "command": command, "assert": {"exit_code": expected}
    })
    assert len(outcomes) == 1
    assert outcomes[0]["passed"] is True
    assert f"actual={expected}" in outcomes[0]["detail"]


def test_exit_code_rejects_nos_even_if_command_could_print_marker(monkeypatch):
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [
        {"name": "r1", "kind": "arista_ceos", "mgmt_ip": "192.0.2.1"}
    ])

    def unexpected_dispatch(*args, **kwargs):
        pytest.fail("unsupported exit_code must be rejected before executing a command")

    monkeypatch.setattr(server, "_dispatch_command", unexpected_dispatch)
    outcomes = server._run_test_case("x", {"command": "show version", "assert": {"exit_code": 0}})
    assert outcomes[0]["passed"] is False
    assert "linux kind" in outcomes[0]["detail"]


@pytest.mark.parametrize("bad_yaml", [
    "lab: [", "scalar", "lab: x\ntests: [null]", "lab: x\ntests: text",
    "tests: []", "lab: 42\ntests: []",
])
def test_invalid_test_file_prevents_all_pass_verdict(tmp_path, monkeypatch, bad_yaml):
    valid = tmp_path / "valid"
    valid.mkdir()
    (valid / "test.yml").write_text("lab: x\ntests: [{command: uptime}]", encoding="utf-8")
    (tmp_path / "test.yml").write_text(bad_yaml, encoding="utf-8")
    monkeypatch.setattr(server, "_run_test_case", lambda lab, case: [
        {"test": "valid", "node": "r1", "passed": True, "detail": "OK"}
    ])
    report = server.run_topology_tests(str(tmp_path))
    assert report["data"]["verdict"] == "FAILURES"
    assert report["counts"]["succeeded"] == report["counts"]["failed"] == 1
    assert report["status"] == "partial"


@pytest.mark.parametrize("case", [
    {"command": 42}, {"command": "uptime", "assert": "bad"},
    {"command": "uptime", "nodes": 42},
])
def test_invalid_case_fields_fail_before_inspection(monkeypatch, case):
    def unexpected_inspect(lab):
        pytest.fail("invalid test case must not contact a lab")

    monkeypatch.setattr(server, "_inspect_nodes", unexpected_inspect)
    assert server._run_test_case("x", case)[0]["passed"] is False
