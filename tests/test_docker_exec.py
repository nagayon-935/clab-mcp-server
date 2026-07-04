from types import SimpleNamespace

import pytest

import server


def test_docker_exec_argv_local_without_clab_host(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", None)
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    assert server._docker_exec_argv("clab-x-r1", "uptime") == [
        "docker",
        "exec",
        "clab-x-r1",
        "sh",
        "-c",
        "uptime",
    ]


def test_docker_exec_argv_adds_sudo_when_enabled(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", None)
    monkeypatch.setattr(server, "CLAB_SUDO", True)
    argv = server._docker_exec_argv("clab-x-r1", "uptime")
    assert argv[0] == "sudo"
    assert argv[1:] == ["docker", "exec", "clab-x-r1", "sh", "-c", "uptime"]


def test_docker_exec_argv_wraps_in_ssh_when_host_set(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", "clab-host")
    monkeypatch.setattr(server, "CLAB_SSH_USER", None)
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    argv = server._docker_exec_argv("clab-x-r1", "uptime")
    assert argv[:2] == ["ssh", "clab-host"]
    assert argv[2] == "docker exec clab-x-r1 sh -c uptime"


def test_run_docker_exec_returns_stdout_on_success(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", None)
    monkeypatch.setattr(server, "CLAB_SUDO", False)

    def fake_run(argv, timeout, label):
        assert argv == ["docker", "exec", "clab-x-r1", "sh", "-c", "ip addr"]
        return SimpleNamespace(returncode=0, stdout="eth0: ...\n", stderr="")

    monkeypatch.setattr(server, "_run_argv", fake_run)
    output = server._run_docker_exec("clab-x-r1", "ip addr", timeout=60)
    assert output == "eth0: ...\n"


def test_run_docker_exec_raises_runtime_error_on_nonzero_exit(monkeypatch):
    def fake_run(argv, timeout, label):
        return SimpleNamespace(returncode=126, stdout="", stderr="OCI runtime exec failed")

    monkeypatch.setattr(server, "_run_argv", fake_run)
    with pytest.raises(RuntimeError, match="OCI runtime exec failed"):
        server._run_docker_exec("clab-x-r1", "vtysh -c 'show ip bgp summary'", timeout=60)


def test_run_command_task_dispatches_linux_kind_to_docker_exec(monkeypatch):
    """linux kind は Netmiko(SSH) ではなく docker exec 経由で実行されること。"""
    calls = []

    def fake_run_docker_exec(container, command, timeout):
        calls.append((container, command))
        return "hello\n"

    monkeypatch.setattr(server, "_run_docker_exec", fake_run_docker_exec)

    host = SimpleNamespace(data={"kind": "linux", "container": "clab-x-srv1"})
    task = SimpleNamespace(host=host)

    result = server._run_command_task(task, "interfaces", use_textfsm=True)

    assert calls == [("clab-x-srv1", "ip addr")]
    assert result.result == {"command": "ip addr", "output": "hello\n"}


def test_dispatch_command_uses_docker_exec_for_linux_kind(monkeypatch):
    """run_topology_tests の _test_task も共有する _dispatch_command が
    linux kind を docker exec に振り分けること（テストエンジンでの漏れ防止）。
    """
    calls = []

    def fake_run_docker_exec(container, command, timeout):
        calls.append((container, command))
        return "output\n"

    monkeypatch.setattr(server, "_run_docker_exec", fake_run_docker_exec)

    host = SimpleNamespace(data={"kind": "linux", "container": "clab-x-r1"})
    task = SimpleNamespace(host=host)

    output = server._dispatch_command(task, "linux", "ip addr; echo __RC__=$?", use_textfsm=True)

    assert calls == [("clab-x-r1", "ip addr; echo __RC__=$?")]
    assert output == "output\n"


def test_collect_config_task_linux_kind_fetches_via_vtysh(monkeypatch):
    def fake_run_docker_exec(container, command, timeout):
        assert container == "clab-x-r1"
        assert command == server.LINUX_CONFIG_COMMAND
        return "hostname r1\n"

    monkeypatch.setattr(server, "_run_docker_exec", fake_run_docker_exec)

    host = SimpleNamespace(data={"kind": "linux", "container": "clab-x-r1"}, name="r1")
    task = SimpleNamespace(host=host)

    result = server._collect_config_task(task)

    assert result.result == "hostname r1\n"
    assert result.failed is False


def test_collect_config_task_linux_kind_skips_when_vtysh_unavailable(monkeypatch):
    """vtysh を持たないプレーンな linux コンテナは取得対象外としてスキップすること。"""

    def fake_run_docker_exec(container, command, timeout):
        raise RuntimeError("docker exec 失敗 (rc=127): vtysh: not found")

    monkeypatch.setattr(server, "_run_docker_exec", fake_run_docker_exec)

    host = SimpleNamespace(data={"kind": "linux", "container": "clab-x-sw1"}, name="sw1")
    task = SimpleNamespace(host=host)

    result = server._collect_config_task(task)

    assert result.result == ""
    assert result.failed is False


def test_collect_config_task_skips_unsupported_kind_without_opening_connection():
    """KIND_COMMAND 未定義 kind は get_connection を呼ばずスキップすること
    （kind 判定前に無条件で Netmiko 接続を開いていた回帰を防ぐ）。
    """
    calls = []

    def fake_get_connection(name, config):
        calls.append((name, config))
        return SimpleNamespace()

    host = SimpleNamespace(
        data={"kind": "some_unsupported_kind"},
        name="r1",
        get_connection=fake_get_connection,
    )
    task = SimpleNamespace(host=host, nornir=SimpleNamespace(config=object()))

    result = server._collect_config_task(task)

    assert calls == []
    assert result.result == ""
    assert result.failed is False
