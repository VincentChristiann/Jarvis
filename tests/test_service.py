import sys

import pytest

from jarvis import service


def test_systemd_user_unit():
    u = service.render_systemd("hub", "/home/me/.jarvis")
    assert "ExecStart=" in u and "hub serve" in u and "--home /home/me/.jarvis" in u
    assert "Restart=always" in u and "NoNewPrivileges=yes" in u
    assert "WantedBy=default.target" in u and "User=" not in u


def test_systemd_quotes_paths_with_spaces():
    u = service.render_systemd("device", "/home/my user/.jarvis")
    assert '"/home/my user/.jarvis"' in u and "device run" in u


def test_system_unit_requires_user():
    with pytest.raises(ValueError):
        service.render_systemd("hub", None, system_wide=True)
    assert "User=jarvis" in service.render_systemd("hub", None, True, "jarvis")


def test_windows_task_args_are_a_list_not_a_shell_string():
    args = service.windows_task_args("device", r"C:\Users\Me\.jarvis")
    assert args[0] == "schtasks" and "/SC" in args and "ONLOGON" in args and "LIMITED" in args
    assert "device run" in args[args.index("/TR") + 1]


def test_dry_run_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = service.install("hub", str(tmp_path), dry_run=True)
    assert out
    assert not (tmp_path / ".config").exists()


def test_install_runs_expected_commands(tmp_path, monkeypatch):
    if not sys.platform.startswith("linux"):
        pytest.skip("linux only")
    monkeypatch.setenv("HOME", str(tmp_path))
    calls = []
    service.install("device", str(tmp_path), run=lambda a, **k: calls.append(a))
    assert ["systemctl", "--user", "enable", "--now", "jarvis-device"] in calls
    assert (tmp_path / ".config/systemd/user/jarvis-device.service").exists()
