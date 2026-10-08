"""Start the 24/7 service automatically when you sign in to Windows.

Adds a small script to your personal Startup folder - no administrator rights, no system
settings changed. Remove it with `topstep-bot autostart off`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SCRIPT_NAME = "Topstep Bot.cmd"


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    if sys.platform != "win32" or not appdata:
        raise OSError("Automatic start is only supported on Windows")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def script_path() -> Path:
    return startup_dir() / SCRIPT_NAME


def build_script(project_dir: Path, config_path: Path) -> str:
    exe = Path(sys.executable).with_name("topstep-bot.exe")
    launcher = f'"{exe}"' if exe.exists() else f'"{sys.executable}" -m topstep_bot'
    return (
        "@echo off\r\n"
        "REM Starts Topstep Bot (dashboard + Telegram + the bot) when you sign in to Windows.\r\n"
        "REM Remove with: topstep-bot autostart off  (or delete this file)\r\n"
        f'cd /d "{project_dir}"\r\n'
        "REM Give the network a moment to come up after sign-in.\r\n"
        "timeout /t 30 /nobreak >nul\r\n"
        f'start "Topstep Bot" /min {launcher} -c "{config_path}" start --yes\r\n'
    )


def enable(config_path: Path) -> Path:
    config_path = config_path.resolve()
    path = script_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_script(config_path.parent, config_path), encoding="utf-8")
    return path


def disable() -> bool:
    path = script_path()
    if path.exists():
        path.unlink()
        return True
    return False


def is_enabled() -> bool:
    try:
        return script_path().exists()
    except OSError:
        return False
