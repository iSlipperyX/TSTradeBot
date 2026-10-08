"""Keep the computer from sleeping while the bot runs (Windows).

Uses the same per-process request media players use (SetThreadExecutionState). It changes no
system settings and ends automatically when the bot exits. It cannot stop sleep caused by
closing a laptop lid, pressing the power button, or a Windows Update restart.
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from typing import Iterator

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
