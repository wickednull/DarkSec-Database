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


def test_easy_lan_access_sets_cookies_for_private_clients():
    from fastapi.testclient import TestClient

    from darksec.server import COOKIE, create_app

    app = create_app(token="lan-token", no_index=True, lan=True, lan_ip="192.168.1.10")
    with TestClient(app, base_url="http://192.168.1.10",
                    client=("192.168.1.50", 50000)) as client:
        assert client.get("/").status_code == 200
        assert COOKIE in client.cookies
        assert client.get("/api/status").status_code == 200

    with TestClient(app, base_url="http://192.168.1.10",
                    client=("8.8.8.8", 50000)) as public:
        public.get("/")
        assert COOKIE not in public.cookies
        assert public.get("/api/status").status_code == 401


def test_easy_lan_access_can_be_disabled():
    from fastapi.testclient import TestClient

    from darksec.server import COOKIE, create_app

    app = create_app(token="lan-token", no_index=True, lan=True, lan_ip="192.168.1.10")
    with TestClient(app, base_url="http://192.168.1.10",
                    client=("192.168.1.50", 50000)) as client:
        app.state.lan_easy_access = False
        client.get("/")
        assert COOKIE not in client.cookies
        assert client.get("/api/status").status_code == 401


def test_lan_listener_reports_http_scheme():
    from darksec.lan import LanManager

    manager = LanManager()
    manager.enabled = True
    manager.ip = "192.168.1.10"
    manager.port = 8080
    status = manager.status()
    assert status["scheme"] == "http"
    assert status["url"] == "http://192.168.1.10:8080/"


def test_lan_settings_validation(client):
    old = client.get("/api/settings").json()
    try:
        r = client.put("/api/settings", json={
            "lan_port": 8099, "lan_easy_access": False, "lan_tls": False,
        })
        assert r.status_code == 200
        updated = r.json()
        assert updated["lan_port"] == 8099
        assert updated["lan_easy_access"] is False
        assert client.put("/api/settings", json={"lan_port": 70000}).status_code == 400
    finally:
        client.put("/api/settings", json={
            "lan_port": old.get("lan_port", 8080),
            "lan_easy_access": old.get("lan_easy_access", True),
            "lan_tls": old.get("lan_tls", False),
        })
