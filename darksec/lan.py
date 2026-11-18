"""Optional LAN / mobile access listener.

The main DarkSec server stays loopback-only. When the operator enables LAN
access (Settings or --lan), this manager starts a *second* listener that serves
the same application, so a phone or tablet on the trusted local network can
reach the console without restarting. Plain HTTP is the default; TLS is
optional and only useful when a trusted certificate is installed.
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
        self.scheme: str = "http"
        self.enabled: bool = False
        self.error: str | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None

    def _free_port(self) -> int:
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("0.0.0.0", 0))
            return sock.getsockname()[1]

    def _port_available(self, port: int) -> bool:
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False

    def start(self, app, ip: str | None = None, port: int | None = None,
              use_tls: bool = False) -> bool:
        if self._thread and self._thread.is_alive():
            self.enabled = True
            return True
        self.error = None
        try:
            self.ip = ip or tls.detect_lan_ip()
            self.port = port or self._free_port()
            if not self._port_available(self.port):
                raise RuntimeError(
                    f"LAN port {self.port} is already in use or not permitted; "
                    "choose another port in Settings → LAN / mobile access."
                )
            self.scheme = "https" if use_tls else "http"
            kwargs = dict(
                host="0.0.0.0", port=self.port,
                log_level="warning", access_log=False, server_header=False,
                lifespan="off",
            )
            if use_tls:
                cert_path, key_path = tls.ensure_cert(self.ip)
                kwargs.update(ssl_certfile=str(cert_path), ssl_keyfile=str(key_path))
            config = uvicorn.Config(app, **kwargs)
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

    def adopt_external(self, ip: str | None, port: int | None, scheme: str = "http") -> None:
        """Represent a LAN listener started by the CLI (no second socket)."""
        self.ip = ip
        self.port = port
        self.scheme = scheme
        self.enabled = True
        self.error = None

    def stop(self) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        thread = self._thread
        # Clear state before joining: the request that disables LAN access can
        # itself be served by the LAN listener, so joining must not target the
        # current thread. uvicorn will finish the response and exit on its own.
        self._server = None
        self._thread = None
        self.enabled = False
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=5)

    def status(self, app=None) -> dict:
        ip = self.ip
        port = self.port
        url = f"{self.scheme}://{ip}:{port}/" if (ip and port and self.enabled) else None
        allowed_hosts = getattr(app.state, "allowed_hosts", None) if app is not None else None
        return {
            "enabled": self.enabled,
            "ip": ip,
            "port": port,
            "scheme": self.scheme,
            "url": url,
            "error": self.error,
        }
