import pytest

import server


def test_strip_cidr_removes_prefix_length():
    assert server._strip_cidr("172.20.20.2/24") == "172.20.20.2"


@pytest.mark.parametrize("value", [None, "", "N/A", "-"])
def test_strip_cidr_treats_placeholder_values_as_none(value):
    assert server._strip_cidr(value) is None


def test_short_node_name_strips_clab_lab_prefix():
    assert server._short_node_name("clab-mylab-r1", "mylab") == "r1"


def test_short_node_name_falls_back_to_last_dash_segment():
    # lab name unknown / doesn't match the prefix
    assert server._short_node_name("clab-mylab-r1", None) == "r1"


def test_short_node_name_returns_input_unchanged_when_not_clab_prefixed():
    assert server._short_node_name("bare-host", None) == "bare-host"


def test_normalize_inspect_json_accepts_plain_list():
    raw = [{"name": "clab-x-r1"}]
    assert server._normalize_inspect_json(raw) == raw


def test_normalize_inspect_json_accepts_containers_key():
    raw = {"containers": [{"name": "clab-x-r1"}]}
    assert server._normalize_inspect_json(raw) == raw["containers"]


def test_normalize_inspect_json_accepts_labname_keyed_dict():
    raw = {"mylab": [{"name": "clab-mylab-r1"}, {"name": "clab-mylab-r2"}]}
    result = server._normalize_inspect_json(raw)
    assert result == raw["mylab"]


def test_normalize_inspect_json_raises_on_unrecognized_structure():
    with pytest.raises(RuntimeError):
        server._normalize_inspect_json("not a valid structure")
