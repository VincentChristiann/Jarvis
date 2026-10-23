"""Install Jarvis so it starts by itself: systemd (Linux) or Task Scheduler (Windows).

Rendering is pure (testable); install/uninstall run argument lists, never a shell.
"""
import subprocess
import sys
from pathlib import Path

NAMES = {"hub": "jarvis-hub", "device": "jarvis-device"}


def _command(role: str, home: str | None) -> list[str]:
    base = [sys.executable, "-m", "jarvis"]
    if home:
        base += ["--home", home]
    return base + (["hub", "serve"] if role == "hub" else ["device", "run"])


def _quote_unit(arg: str) -> str:
    return '"' + arg.replace("\\", "\\\\").replace('"', '\\"') + '"' if any(c in arg for c in ' "\\') else arg


def render_systemd(role: str, home: str | None, system_wide=False, user: str | None = None) -> str:
    exec_start = " ".join(_quote_unit(a) for a in _command(role, home))
    lines = [
        "[Unit]", f"Description=Jarvis {role}",
        "After=network-online.target", "Wants=network-online.target", "",
        "[Service]", f"ExecStart={exec_start}", "Restart=always", "RestartSec=3",
        "NoNewPrivileges=yes", "PrivateTmp=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
    ]
    if system_wide:
        if not user:
            raise ValueError("a system-wide unit needs a non-root user to run as")
        lines.append(f"User={user}")
    lines += ["", "[Install]", f"WantedBy={'multi-user.target' if system_wide else 'default.target'}", ""]
    return "\n".join(lines)


def unit_path(role: str, system_wide=False) -> Path:
    base = Path("/etc/systemd/system") if system_wide else Path.home() / ".config/systemd/user"
    return base / f"{NAMES[role]}.service"


def windows_task_args(role: str, home: str | None) -> list[str]:
    """schtasks arguments: runs at logon as the current user, so keys stay in that user's profile."""
    cmd = subprocess.list2cmdline(_command(role, home))
    return ["schtasks", "/Create", "/F", "/TN", NAMES[role], "/TR", cmd, "/SC", "ONLOGON", "/RL", "LIMITED"]


def install(role: str, home: str | None, system_wide=False, user=None, dry_run=False, run=subprocess.run) -> str:
    if sys.platform.startswith("linux"):
        text, path = render_systemd(role, home, system_wide, user), unit_path(role, system_wide)
        if dry_run:
            return f"# {path}\n{text}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        sc = ["systemctl"] + ([] if system_wide else ["--user"])
        run(sc + ["daemon-reload"], check=True)
        run(sc + ["enable", "--now", NAMES[role]], check=True)
        hint = "" if system_wide else (
            "\nTo start before you log in, run once: sudo loginctl enable-linger $USER")
        return f"installed {path}{hint}"
    if sys.platform == "win32":
        args = windows_task_args(role, home)
        if dry_run:
            return " ".join(args)
        run(args, check=True)
        run(["schtasks", "/Run", "/TN", NAMES[role]], check=True)
        return f"scheduled task {NAMES[role]} created (starts at logon)"
    raise RuntimeError("automatic start is supported on Linux and Windows")


def uninstall(role: str, system_wide=False, run=subprocess.run) -> str:
    if sys.platform.startswith("linux"):
        sc = ["systemctl"] + ([] if system_wide else ["--user"])
        run(sc + ["disable", "--now", NAMES[role]], check=False)
        unit_path(role, system_wide).unlink(missing_ok=True)
        run(sc + ["daemon-reload"], check=False)
        return f"removed {NAMES[role]}"
    if sys.platform == "win32":
        run(["schtasks", "/Delete", "/F", "/TN", NAMES[role]], check=False)
        return f"removed {NAMES[role]}"
    raise RuntimeError("unsupported platform")
