import server


def test_alias_resolves_to_kind_specific_command():
    assert server.resolve_command("bgp-summary", "arista_ceos") == "show ip bgp summary"
    assert server.resolve_command("bgp-summary", "linux") == "vtysh -c 'show ip bgp summary'"
    assert server.resolve_command("ip-route", "juniper_crpd") == "cli -c 'show route'"


def test_alias_without_kind_definition_falls_back_to_literal():
    # cisco_iol has no entry under "bgp-summary" in COMMAND_ALIASES
    assert server.resolve_command("bgp-summary", "cisco_iol") == "bgp-summary"


def test_unknown_alias_is_treated_as_literal_command():
    assert server.resolve_command("show version", "cisco_xrd") == "show version"
