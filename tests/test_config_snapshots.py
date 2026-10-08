from datetime import datetime

import server


def _mock_config_collection(monkeypatch, config="hostname r1"):
    monkeypatch.setattr(server, "_inspect_nodes", lambda lab: [])
    monkeypatch.setattr(server, "_build_nornir", lambda nodes: object())
    monkeypatch.setattr(server, "_run_nornir", lambda nr, task: {
        "r1": {"failed": False, "result": config}
    })


def test_snapshot_calls_at_same_time_preserve_both_configs(tmp_path, monkeypatch):
    class FixedDatetime:
        @staticmethod
        def now():
            return datetime(2026, 1, 1)

    monkeypatch.setattr(server, "datetime", FixedDatetime)
    save_dir = tmp_path / "save"
    _mock_config_collection(monkeypatch, "first config")
    first = server.snapshot_and_save_configs("x", save_dir=str(save_dir))
    _mock_config_collection(monkeypatch, "second config")
    second = server.snapshot_and_save_configs("x", save_dir=str(save_dir))

    assert "保存成功: 1" in first
    assert "保存成功: 1" in second
    assert {path.read_text() for path in save_dir.glob("save-*/r1.conf")} == {
        "first config\n", "second config\n"
    }


def test_snapshot_directory_creation_failure_returns_error(tmp_path, monkeypatch):
    _mock_config_collection(monkeypatch)
    save_dir = tmp_path / "file"
    save_dir.write_text("file", encoding="utf-8")
    result = server.snapshot_and_save_configs("x", save_dir=str(save_dir))
    assert "エラー" in result
    assert save_dir.read_text() == "file"


def test_startup_snapshot_validates_topology_before_collecting_configs(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    def unexpected_inspection(lab):
        raise AssertionError("topology validation must precede config collection")

    monkeypatch.setattr(server, "_inspect_nodes", unexpected_inspection)
    assert "トポロジ YAML が必要" in server.snapshot_and_save_configs("x", mode="startup")


def test_latest_snapshot_ignores_files(tmp_path):
    snapshot = tmp_path / "save-20260101"
    snapshot.mkdir()
    (tmp_path / "save-99999999").write_text("file", encoding="utf-8")
    assert server._resolve_snapshot_dir(str(tmp_path), "latest") == str(snapshot)


def test_latest_snapshot_rejects_symlink_outside_save_dir(tmp_path):
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "r1.conf").write_text("unexpected config", encoding="utf-8")
    (save_dir / "save-99999999").symlink_to(outside, target_is_directory=True)
    topo_path = tmp_path / "x.clab.yml"
    topo_path.write_text("name: x\ntopology: {nodes: {r1: {}}}", encoding="utf-8")

    result = server.restore_startup_configs(str(topo_path), save_dir=str(save_dir))
    assert "エラー" in result
    assert not (tmp_path / "startup-configs").exists()


def test_latest_snapshot_handles_glob_characters_in_save_dir(tmp_path):
    save_dir = tmp_path / "save[1]"
    snapshot = save_dir / "save-20260101"
    snapshot.mkdir(parents=True)
    assert server._resolve_snapshot_dir(str(save_dir), "latest") == str(snapshot)
