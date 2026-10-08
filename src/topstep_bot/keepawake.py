"""Keep the computer awake and the bot's window responsive while the bot runs (Windows).

keep_awake: the same per-process request media players use (SetThreadExecutionState). It changes
no system settings and ends automatically when the bot exits. It cannot stop sleep caused by
closing a laptop lid, pressing the power button, or a Windows Update restart.

console_stays_responsive: with Windows' "QuickEdit" console mode (on by default), a single click
inside the bot's window selects text and FREEZES the program until a key is pressed - no stop
management, no risk checks, no Telegram. It is switched off while the bot runs and restored after.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress

log = logging.getLogger(__name__)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set(flags: int) -> bool:
    if sys.platform != "win32":
        return False
    import ctypes

    return bool(ctypes.windll.kernel32.SetThreadExecutionState(flags))


@contextmanager
def keep_awake(enabled: bool = True) -> Iterator[bool]:
    active = enabled and _set(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    if active:
        log.info("Keeping the computer awake while the bot runs")
    elif enabled and sys.platform != "win32":
        log.info("keep_awake only works on Windows; disable sleep in your system settings instead")
    try:
        yield active
    finally:
        if active:
            _set(ES_CONTINUOUS)


ENABLE_QUICK_EDIT_MODE = 0x0040
ENABLE_EXTENDED_FLAGS = 0x0080
STD_INPUT_HANDLE = -10


@contextmanager
def console_stays_responsive() -> Iterator[bool]:
    """Turn off QuickEdit for this console window while the bot runs (no-op elsewhere)."""
    original = None
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
            mode = ctypes.c_uint32()
            if (
                kernel32.GetConsoleMode(handle, ctypes.byref(mode))
                and mode.value & ENABLE_QUICK_EDIT_MODE
                and kernel32.SetConsoleMode(handle, (mode.value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE)
            ):
                original = mode.value
                log.debug("Console QuickEdit turned off while the bot runs")
        except (AttributeError, OSError) as exc:  # not a real console (e.g. output redirected)
            log.debug("Could not change console mode: %s", exc)
    try:
        yield original is not None
    finally:
        if original is not None:
            with suppress(AttributeError, OSError):
                ctypes.windll.kernel32.SetConsoleMode(ctypes.windll.kernel32.GetStdHandle(STD_INPUT_HANDLE), original)
