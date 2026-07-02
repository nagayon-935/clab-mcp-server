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


def test_topo_nodes_and_links_extracts_both_sections():
    nodes, links = server._topo_nodes_and_links(TOPO)
    assert set(nodes.keys()) == {"r1", "r2"}
    assert links == TOPO["topology"]["links"]


def test_topo_nodes_and_links_handles_missing_topology_key():
    nodes, links = server._topo_nodes_and_links({"name": "empty"})
    assert nodes == {}
    assert links == []


def test_startup_path_uses_explicit_startup_config_when_defined():
    path = server._startup_path_for_node(TOPO, "r1", "startup-configs")
    assert path == "configs/r1-custom.cfg"


def test_startup_path_falls_back_to_default_dir_when_undefined():
    path = server._startup_path_for_node(TOPO, "r2", "startup-configs")
    assert path == "startup-configs/r2.conf"


def test_startup_path_falls_back_for_unknown_node():
    path = server._startup_path_for_node(TOPO, "r99", "startup-configs")
    assert path == "startup-configs/r99.conf"
