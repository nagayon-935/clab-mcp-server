import pytest

import server

NODES = [
    {"name": "r1", "container": "clab-x-r1", "kind": "arista_ceos", "mgmt_ip": "172.20.20.2"},
    {"name": "r2", "container": "clab-x-r2", "kind": "juniper_crpd", "mgmt_ip": "172.20.20.3"},
    {"name": "srv1", "container": "clab-x-srv1", "kind": "linux", "mgmt_ip": "172.20.20.4"},
    {"name": "noip", "container": "clab-x-noip", "kind": "linux", "mgmt_ip": None},
]


def test_build_host_returns_none_when_no_mgmt_ip():
    assert server._build_host({"name": "noip", "kind": "linux", "mgmt_ip": None}) is None


def test_build_host_sets_platform_and_data_from_kind():
    name, host = server._build_host(NODES[0])
    assert name == "r1"
    assert host.hostname == "172.20.20.2"
    assert host.platform == "arista_eos"
    assert host.data["kind"] == "arista_ceos"
    assert host.data["container"] == "clab-x-r1"


def test_build_nornir_skips_hosts_without_mgmt_ip():
    nr = server._build_nornir(NODES)
    assert set(nr.inventory.hosts.keys()) == {"r1", "r2", "srv1"}


def test_build_nornir_applies_node_filter_regex():
    nr = server._build_nornir(NODES, node_filter_regex="^r")
    assert set(nr.inventory.hosts.keys()) == {"r1", "r2"}


def test_build_nornir_raises_when_no_hosts_match():
    with pytest.raises(RuntimeError):
        server._build_nornir(NODES, node_filter_regex="^nomatch$")


def test_build_nornir_worker_count_capped_by_host_count():
    nr = server._build_nornir(NODES, node_filter_regex="^r")
    assert nr.runner.num_workers == 2
