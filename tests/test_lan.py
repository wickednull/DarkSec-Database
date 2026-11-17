"""LAN listener lifecycle.

Disabling LAN access from a page served by the LAN listener itself used to
raise ``RuntimeError: cannot join current thread``. Stopping must mark the
server for exit without trying to join the request's own thread.
"""
from __future__ import annotations

import threading

from darksec.lan import LanManager


class _FakeServer:
    def __init__(self) -> None:
        self.should_exit = False


def test_stop_from_listener_thread_does_not_join_itself():
    manager = LanManager()
    server = _FakeServer()
    manager._server = server
    manager._thread = threading.current_thread()
    manager.enabled = True

    manager.stop()

    assert server.should_exit is True
    assert manager._server is None
    assert manager._thread is None
    assert manager.enabled is False


def test_stop_joins_a_live_listener_thread():
    manager = LanManager()
    server = _FakeServer()
    release = threading.Event()

    def run() -> None:
        release.wait(2)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    manager._server = server
    manager._thread = thread
    manager.enabled = True
    release.set()

    manager.stop()

    assert server.should_exit is True
    assert not thread.is_alive()
