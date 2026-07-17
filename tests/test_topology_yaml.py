import os

import pytest

import server

TOPO = {
    "name": "mylab",
    "topology": {
        "nodes": {
            "r1": {"kind": "arista_ceos", "startup-config": "configs/r1-custom.cfg"},
            "r2": {"kind": "linux"},
        },
        "links": [{"endpoints": ["r1:eth1", "r2:eth1"]}],
    },
}

TOPO_TRAVERSAL = {
    "name": "evil",
    "topology": {
        "nodes": {
            "r1": {"kind": "linux", "startup-config": "../../../../etc/passwd"},
            "r2": {"kind": "linux", "startup-config": "/etc/passwd"},
        },
        "links": [],
    },
}


def test_find_topo_for_lab_returns_none_when_no_name_matches(tmp_path, monkeypatch):
    """name が一致するトポロジが無い場合、無関係な別ラボの YAML へ
    フォールバックしないこと（誤って別ラボの startup-config を上書きする
    事故を防ぐため、最初に見つかった候補を返す旧フォールバックは廃止した）。"""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "other.clab.yml").write_text("name: other-lab\ntopology: {}\n")

    assert server._find_topo_for_lab("mylab") is None


def test_find_topo_for_lab_returns_matching_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "other.clab.yml").write_text("name: other-lab\ntopology: {}\n")
    (tmp_path / "mylab.clab.yml").write_text("name: mylab\ntopology: {}\n")

    result = server._find_topo_for_lab("mylab")

    assert result == "mylab.clab.yml"


def test_topo_nodes_and_links_extracts_both_sections():
    nodes, links = server._topo_nodes_and_links(TOPO)
    assert set(nodes.keys()) == {"r1", "r2"}
    assert links == TOPO["topology"]["links"]


def test_topo_nodes_and_links_handles_missing_topology_key():
    nodes, links = server._topo_nodes_and_links({"name": "empty"})
    assert nodes == {}
    assert links == []


def test_startup_path_uses_explicit_startup_config_when_defined(tmp_path):
    path = server._startup_path_for_node(TOPO, "r1", "startup-configs", str(tmp_path))
    assert path == os.path.realpath(str(tmp_path / "configs" / "r1-custom.cfg"))


def test_startup_path_falls_back_to_default_dir_when_undefined(tmp_path):
    path = server._startup_path_for_node(TOPO, "r2", "startup-configs", str(tmp_path))
    assert path == os.path.realpath(str(tmp_path / "startup-configs" / "r2.conf"))


def test_startup_path_falls_back_for_unknown_node(tmp_path):
    path = server._startup_path_for_node(TOPO, "r99", "startup-configs", str(tmp_path))
    assert path == os.path.realpath(str(tmp_path / "startup-configs" / "r99.conf"))


@pytest.mark.parametrize("node_name", ["r1", "r2"])
def test_startup_path_rejects_traversal_outside_base_dir(tmp_path, node_name):
    with pytest.raises(ValueError):
        server._startup_path_for_node(
            TOPO_TRAVERSAL, node_name, "startup-configs", str(tmp_path)
        )


def test_startup_path_rejects_node_name_escaping_default_dir(tmp_path):
    evil_topo = {"name": "evil", "topology": {"nodes": {"../../etc/passwd": {}}}}
    with pytest.raises(ValueError):
        server._startup_path_for_node(
            evil_topo, "../../etc/passwd", "startup-configs", str(tmp_path)
        )


def test_safe_join_allows_base_dir_itself(tmp_path):
    assert server._safe_join(str(tmp_path), ".") == os.path.realpath(str(tmp_path))


def test_safe_join_allows_nested_relative_path(tmp_path):
    result = server._safe_join(str(tmp_path), "sub", "file.txt")
    assert result == os.path.realpath(str(tmp_path / "sub" / "file.txt"))


@pytest.mark.parametrize("evil", ["../escape", "../../etc/passwd", "/etc/passwd"])
def test_safe_join_rejects_escape_attempts(tmp_path, evil):
    with pytest.raises(ValueError):
        server._safe_join(str(tmp_path), evil)
