"""Shared lifecycle helpers for the demo agents.

Adds graceful shutdown on SIGTERM/SIGINT so agents can be stopped cleanly
from outside without exiting on idle. Pairs with --serve-forever on the CLI.
"""
from __future__ import annotations

import signal
from typing import Callable


class ShutdownFlag:
    """Mutable flag flipped by signal handlers. Agents check it each tick."""

    def __init__(self, on_shutdown: Callable[[], None] | None = None) -> None:
        self._set = False
        self._on_shutdown = on_shutdown

    def is_set(self) -> bool:
        return self._set

    def request(self) -> None:
        if not self._set:
            self._set = True
            if self._on_shutdown is not None:
                self._on_shutdown()


def install_signal_handlers(flag: ShutdownFlag) -> None:
    """Wire SIGTERM and SIGINT to flip the shutdown flag.

    Safe to call once at startup. Re-raising signals is intentionally avoided
    so the main loop can finish its current message before exiting.
    """
    def _handler(signum, frame):
        flag.request()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)
