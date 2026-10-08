"""Last-line Topstep compliance guards that sit in front of the order path.

The RiskManager decides *whether* and *how big* to trade. These guards are a second, independent
check right before an order goes out, so a bug elsewhere still can't make the bot break a rule:

* ``OrderGuard.check_entry`` - a new entry may never take the position past Topstep's cap.
* ``OrderGuard.record_action`` - an order-rate circuit breaker. Topstep prohibits high-frequency
  trading; a runaway loop that sends orders too quickly trips the breaker and stops new entries
  (exits and flattening are never blocked).
* ``api_trading_block`` - Live Funded Accounts may not trade through the ProjectX API.
* ``hosting_warning`` - Topstep's terms prohibit trading from a VPS, VPN or remote server.
"""

from __future__ import annotations

import os
import platform
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from topstep_bot.models import Account

# Far above what any strategy here needs (a trade is about 3 actions), far below anything that
# could be mistaken for high-frequency trading.
DEFAULT_MAX_ACTIONS_PER_MINUTE = 30
DEFAULT_MAX_ENTRIES_PER_DAY = 20


class OrderGuard:
    def __init__(
        self,
        max_contracts: Callable[[], int],
        *,
        max_actions_per_minute: int = DEFAULT_MAX_ACTIONS_PER_MINUTE,
        max_entries_per_day: int = DEFAULT_MAX_ENTRIES_PER_DAY,
    ):
        self.max_contracts = max_contracts
        self.max_actions_per_minute = max_actions_per_minute
        self.max_entries_per_day = max_entries_per_day
        self._actions: deque[datetime] = deque()
        self._entries: dict[str, int] = {}
        self.tripped: str | None = None

    def check_entry(self, size: int, current_position: int, now: datetime) -> str | None:
        """None if an entry of ``size`` contracts may be sent now, else why not."""
        if self.tripped:
            return f"order guard tripped: {self.tripped}"
        if size < 1:
            return "order size must be at least 1 contract"
        cap = self.max_contracts()
        if abs(current_position) + size > cap:
            return (f"{size} contract(s) on top of a {abs(current_position)}-contract position would exceed "
                    f"Topstep's {cap}-contract limit")
        day = now.date().isoformat()
        if self._entries.get(day, 0) >= self.max_entries_per_day:
            return f"{self.max_entries_per_day} entries already sent today (safety limit)"
        return None

    def record_entry(self, now: datetime) -> None:
        day = now.date().isoformat()
        self._entries = {day: self._entries.get(day, 0) + 1}

    def record_action(self, now: datetime) -> str | None:
        """Count one order action (place / modify / cancel). Returns the trip reason the first time it trips."""
        self._actions.append(now)
        while self._actions and now - self._actions[0] > timedelta(minutes=1):
            self._actions.popleft()
        if self.tripped is None and len(self._actions) > self.max_actions_per_minute:
            self.tripped = (f"{len(self._actions)} order actions in one minute (limit {self.max_actions_per_minute}) - "
                            "stopped to avoid anything resembling high-frequency trading")
            return self.tripped
        return None

    def reset(self) -> None:
        self._actions.clear()
        self.tripped = None


def api_trading_block(account: Account) -> str | None:
    """Topstep does not allow Live Funded Accounts to trade through the API."""
    if not account.simulated:
        return (f"{account.name} is a Live (real-money) account. Topstep does not allow Live Funded Accounts to "
                "trade through the TopstepX API, so the bot will not trade it. Use it for Combine and Express "
                "Funded accounts only.")
    return None


# DMI vendor / product strings of common cloud and VPS providers (Linux: /sys/class/dmi/id).
_CLOUD_MARKERS = (
    "amazon ec2", "google compute engine", "digitalocean", "vultr", "linode", "hetzner", "ovh", "scaleway",
    "alibaba cloud", "oracle cloud", "openstack", "virtual machine",  # Hyper-V / Azure report "Virtual Machine"
    "kvm", "qemu", "xen", "bochs",
)


def _dmi_strings(root: Path = Path("/sys/class/dmi/id")) -> list[str]:
    values = []
    for name in ("sys_vendor", "product_name", "board_vendor", "bios_vendor"):
        try:
            values.append((root / name).read_text(encoding="utf-8", errors="ignore").strip().lower())
        except OSError:
            continue
    return values


def _windows_bios_strings() -> list[str]:
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:
        return []
    values = []
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\BIOS") as key:
            for name in ("SystemManufacturer", "SystemProductName", "BaseBoardManufacturer"):
                try:
                    values.append(str(winreg.QueryValueEx(key, name)[0]).strip().lower())
                except OSError:
                    continue
    except OSError:
        return []
    return values


def hosting_warning(dmi_root: Path = Path("/sys/class/dmi/id"), env: dict[str, str] | None = None) -> str | None:
    """A warning if this computer looks like a VPS / cloud server or a remote-desktop session.

    Best effort only: it can't see VPNs and can't prove a machine is personal. Topstep's rule is
    that all trading activity must originate from your personal device.
    """
    env = dict(os.environ) if env is None else env
    if env.get("SESSIONNAME", "").upper().startswith("RDP-"):
        return "this looks like a Remote Desktop session"
    if dmi_root != Path("/sys/class/dmi/id") or platform.system() == "Linux":
        values = _dmi_strings(dmi_root)
    elif platform.system() == "Windows":
        values = _windows_bios_strings()
    else:
        values = []
    for value in values:
        for marker in _CLOUD_MARKERS:
            if marker in value:
                return f"this computer looks like a virtual machine or cloud server ('{value}')"
    return None
