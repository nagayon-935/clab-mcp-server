import subprocess
from unittest.mock import Mock

import pytest

import server


@pytest.mark.parametrize("field, value", [
    ("remote_host", "-V"), ("remote_host", "host\n"),
    ("container_name", "-namespace"), ("interface_name", "eth1\n"),
])
def test_capture_rejects_options_and_newlines_before_launch(monkeypatch, field, value):
    launch = Mock()
    monkeypatch.setattr(server.subprocess, "Popen", launch)
    kwargs = {"remote_host": "host", "container_name": "clab-x-r1", "interface_name": "eth1"}
    kwargs[field] = value
    assert "エラー" in server.trigger_packet_capture(**kwargs)
    launch.assert_not_called()


@pytest.mark.parametrize("error", [FileNotFoundError("wireshark"), OSError("launch failure")])
def test_capture_cleans_up_ssh_if_wireshark_launch_fails(monkeypatch, error):
    ssh = Mock()
    ssh.poll.return_value = None
    launch = Mock(side_effect=[ssh, error])
    monkeypatch.setattr(server, "_find_wireshark", lambda: "/fake/wireshark")
    monkeypatch.setattr(server.subprocess, "Popen", launch)

    result = server.trigger_packet_capture("host", "clab-x-r1", "eth1")
    assert "エラー" in result
    ssh.stdout.close.assert_called_once()
    ssh.terminate.assert_called_once()
    ssh.wait.assert_called_once_with(timeout=5)


def test_stop_capture_kills_and_reaps_process_if_termination_times_out():
    proc = Mock()
    proc.poll.return_value = None
    proc.wait.side_effect = [subprocess.TimeoutExpired("ssh", 5), 0]
    server._stop_capture_process(proc)
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()
    assert proc.wait.call_count == 2
