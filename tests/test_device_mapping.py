import server


def test_known_kind_maps_via_clab_to_platform_table():
    assert server._netmiko_device_type("juniper_crpd") == "juniper_junos"
    assert server._netmiko_device_type("arista_ceos") == "arista_eos"
    assert server._netmiko_device_type("cisco_xrd") == "cisco_xr"
    assert server._netmiko_device_type("cisco_n9kv") == "cisco_nxos"


def test_linux_kind_maps_to_linux_device_type():
    assert server._netmiko_device_type("linux") == "linux"


def test_unmapped_kind_falls_back_to_heuristic():
    # cisco_csr1000v is not in CLAB_TO_PLATFORM; heuristic should catch "cisco*"
    assert server._netmiko_device_type("cisco_csr1000v") == "cisco_ios"
    assert server._netmiko_device_type("juniper_vsrx") == "juniper_junos"


def test_credentials_use_kind_defaults_by_default():
    user, password = server._credentials_for("arista_ceos")
    assert (user, password) == ("admin", "admin")

    user, password = server._credentials_for("juniper_crpd")
    assert (user, password) == ("root", "clab123")


def test_credentials_can_be_overridden_by_env_vars(monkeypatch):
    monkeypatch.setenv("CLAB_USER_ARISTA_CEOS", "custom-user")
    monkeypatch.setenv("CLAB_PASS_ARISTA_CEOS", "custom-pass")

    user, password = server._credentials_for("arista_ceos")

    assert (user, password) == ("custom-user", "custom-pass")
