import shlex
from types import SimpleNamespace

import server


def test_clab_argv_local_without_clab_host(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", None)
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    assert server._clab_argv(["inspect", "--name", "mylab"]) == [
        "clab",
        "inspect",
        "--name",
        "mylab",
    ]


def test_clab_argv_adds_sudo_when_enabled(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", None)
    monkeypatch.setattr(server, "CLAB_SUDO", True)
    assert server._clab_argv(["deploy"])[0] == "sudo"


def test_clab_argv_wraps_in_ssh_when_host_set(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", "clab-host")
    monkeypatch.setattr(server, "CLAB_SSH_USER", None)
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    argv = server._clab_argv(["inspect", "--name", "mylab"])
    assert argv[0] == "ssh"
    assert argv[-2:] == ["clab-host", "clab inspect --name mylab"]
    # non-interactive ssh: never blocks on host-key/password prompts
    assert "-o" in argv and "BatchMode=yes" in argv


def test_clab_argv_uses_user_at_host_when_ssh_user_set(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", "clab-host")
    monkeypatch.setattr(server, "CLAB_SSH_USER", "admin")
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    argv = server._clab_argv(["inspect"])
    assert argv[-2] == "admin@clab-host"


def test_clab_argv_shell_quotes_args_against_injection(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", "clab-host")
    monkeypatch.setattr(server, "CLAB_SSH_USER", None)
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    argv = server._clab_argv(["deploy", "-t", "; rm -rf / #"])
    # Exactly one remote-command argv element passed to ssh (not shell=True
    # locally), so a POSIX shell on the remote end must parse it back to the
    # literal tokens below rather than executing an injected command.
    assert shlex.split(argv[-1]) == ["clab", "deploy", "-t", "; rm -rf / #"]


def test_clab_argv_wraps_remote_command_in_timeout_when_timeout_given(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", "clab-host")
    monkeypatch.setattr(server, "CLAB_SSH_USER", None)
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    argv = server._clab_argv(["inspect"], timeout=42)
    assert shlex.split(argv[-1]) == ["timeout", "42", "clab", "inspect"]


def test_clab_argv_sudo_is_noninteractive(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", None)
    monkeypatch.setattr(server, "CLAB_SUDO", True)
    assert server._clab_argv(["deploy"])[:2] == ["sudo", "-n"]


def test_run_argv_never_inherits_stdin(monkeypatch):
    """子プロセス（ssh 等）が MCP サーバーの stdin（LLM との JSON-RPC ストリーム）
    を継承・消費しないこと。継承したままだと通信破壊やパスワードプロンプト
    待ちの無限ハングにつながる。"""
    captured_kwargs = {}

    def fake_run(argv, **kwargs):
        captured_kwargs.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server._run_argv(["clab", "inspect"], timeout=10, label="test")

    assert captured_kwargs["stdin"] == server.subprocess.DEVNULL


def test_clab_host_fs_warning_empty_when_no_clab_host(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", None)
    assert server._clab_host_fs_warning() == []


def test_clab_host_fs_warning_present_when_clab_host_set(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", "clab-host")
    warning = server._clab_host_fs_warning()
    assert len(warning) == 1
    assert "clab-host" in warning[0]


def test_restore_startup_configs_rejects_snapshot_name_traversal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    topo_path = tmp_path / "mylab.clab.yml"
    topo_path.write_text("name: mylab\ntopology:\n  nodes:\n    r1: {kind: linux}\n")

    result = server.restore_startup_configs(
        topo_path=str(topo_path), snapshot_name="../../../etc", save_dir="save"
    )

    assert "エラー" in result


def test_restore_startup_configs_rejects_startup_config_outside_base_dir(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    labdir = tmp_path / "labdir"
    labdir.mkdir()
    outside_target = tmp_path / "outside_secret.cfg"
    topo_path = labdir / "mylab.clab.yml"
    topo_path.write_text(
        "name: mylab\n"
        "topology:\n"
        "  nodes:\n"
        f"    r1: {{kind: linux, startup-config: '{outside_target}'}}\n"
    )
    snap_dir = labdir / "save" / "save-20260101-000000"
    snap_dir.mkdir(parents=True)
    (snap_dir / "r1.conf").write_text("malicious content\n")

    result = server.restore_startup_configs(
        topo_path=str(topo_path),
        snapshot_name="save-20260101-000000",
        save_dir=str(labdir / "save"),
    )

    assert "エラー" in result
    assert not outside_target.exists()


def test_restore_startup_configs_writes_dest_for_valid_relative_path(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    topo_path = tmp_path / "mylab.clab.yml"
    topo_path.write_text(
        "name: mylab\n"
        "topology:\n"
        "  nodes:\n"
        "    r1: {kind: linux, startup-config: configs/r1.cfg}\n"
    )
    snap_dir = tmp_path / "save" / "save-20260101-000000"
    snap_dir.mkdir(parents=True)
    (snap_dir / "r1.conf").write_text("hostname r1\n")

    result = server.restore_startup_configs(
        topo_path=str(topo_path), snapshot_name="save-20260101-000000", save_dir="save"
    )

    assert "復元成功: 1" in result
    assert (tmp_path / "configs" / "r1.cfg").read_text() == "hostname r1\n"
