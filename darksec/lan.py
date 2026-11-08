"""Optional LAN / mobile access listener.

The main DarkSec server stays loopback-only. When the operator enables LAN
access (Settings or --lan), this manager starts a *second* HTTPS listener on
the selected interface that serves the same application, so a phone or tablet
on the trusted local network can reach the console without restarting.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import uvicorn

from . import tls


class LanManager:
    def __init__(self) -> None:
        self.ip: str | None = None
        self.port: int | None = None
        self.enabled: bool = False
        self.error: str | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def _free_port(self) -> int:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", 0))
            return sock.getsockname()[1]

    def start(self, app, ip: str | None = None, port: int | None = None) -> bool:
        if self._thread and self._thread.is_alive():
            self.enabled = True
            return True
        self.error = None
        try:
            self.ip = ip or tls.detect_lan_ip()
            self.port = port or self._free_port()
            cert_path, key_path = tls.ensure_cert(self.ip)
            config = uvicorn.Config(
                app, host="0.0.0.0", port=self.port,
                ssl_certfile=str(cert_path), ssl_keyfile=str(key_path),
                log_level="warning", access_log=False, server_header=False,
                lifespan="off",
            )
            server = uvicorn.Server(config)
            self._server = server
            self._thread = threading.Thread(target=server.run, name="darksec-lan", daemon=True)
            self._thread.start()
            # Wait briefly for startup.
            deadline = time.time() + 5
            while time.time() < deadline and not server.started:
                time.sleep(0.1)
            if not server.started:
                raise RuntimeError("LAN listener did not start")
            self.enabled = True
            return True
        except Exception as exc:
            self.enabled = False
            self.error = str(exc)[:300]
            self._server = None
            self._thread = None
            return False

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None
        self.enabled = False

    def status(self, app=None) -> dict:
        ip = self.ip
        port = self.port
        url = f"https://{ip}:{port}/" if (ip and port and self.enabled) else None
        allowed_hosts = getattr(app.state, "allowed_hosts", None) if app is not None else None
        return {
            "enabled": self.enabled,
            "ip": ip,
            "port": port,
            "url": url,
            "error": self.error,
        }
