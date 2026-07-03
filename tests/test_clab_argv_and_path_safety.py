import shlex

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
    assert argv[:2] == ["ssh", "clab-host"]
    assert argv[2] == "clab inspect --name mylab"


def test_clab_argv_uses_user_at_host_when_ssh_user_set(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", "clab-host")
    monkeypatch.setattr(server, "CLAB_SSH_USER", "admin")
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    argv = server._clab_argv(["inspect"])
    assert argv[1] == "admin@clab-host"


def test_clab_argv_shell_quotes_args_against_injection(monkeypatch):
    monkeypatch.setattr(server, "CLAB_HOST", "clab-host")
    monkeypatch.setattr(server, "CLAB_SSH_USER", None)
    monkeypatch.setattr(server, "CLAB_SUDO", False)
    argv = server._clab_argv(["deploy", "-t", "; rm -rf / #"])
    # Exactly one remote-command argv element passed to ssh (not shell=True
    # locally), so a POSIX shell on the remote end must parse it back to the
    # literal tokens below rather than executing an injected command.
    assert len(argv) == 3
    assert shlex.split(argv[2]) == ["clab", "deploy", "-t", "; rm -rf / #"]


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
