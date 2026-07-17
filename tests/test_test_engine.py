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
