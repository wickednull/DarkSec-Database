"""Temporary local HTTPS bridge for browser-based phone GPS.

This follows the Hak5 Mobile2GPS pattern without opening DarkSec's main API:
the operator chooses one local IPv4 interface, a short-lived HTTPS listener
serves a tiny local page, and the phone browser posts permissioned GPS fixes to
that listener. No cloud relay, installed phone app, or location persistence is
used by this module.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import secrets
import socket
import ssl
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import psutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import config
from .db import utcnow

PAIR_SECONDS = 5 * 60
FIX_FRESH_SECONDS = 30
MIN_FIX_INTERVAL_SECONDS = 1.0
MAX_BODY_BYTES = 4096
MAX_HORIZONTAL_ACCURACY_M = 100_000.0
PHONE_PAGE = config.UI_DIR / "phone-gps.html"


class PhoneGpsError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def local_ipv4_interfaces() -> list[dict[str, str]]:
    """Return private, non-loopback IPv4 addresses for explicit interface choice."""
    out = []
    stats = psutil.net_if_stats()
    for name, addresses in psutil.net_if_addrs().items():
        if name in stats and not stats[name].isup:
            continue
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            try:
                ip = ipaddress.ip_address(address.address)
            except ValueError:
                continue
            if not isinstance(ip, ipaddress.IPv4Address) or not ip.is_private or ip.is_loopback or ip.is_link_local:
                continue
            out.append({"interface": name, "ip": str(ip)})
    def interface_rank(name: str) -> int:
        lowered = name.lower()
        if lowered.startswith(("wl", "wifi", "wlan")):
            return 0
        if lowered.startswith(("en", "eth", "ethernet")):
            return 1
        if any(part in lowered for part in ("docker", "lxc", "veth", "virbr", "vmnet", "tailscale", "tun", "tap", "wg")):
            return 9
        return 2

    return sorted(out, key=lambda row: (interface_rank(row["interface"]), row["interface"].lower(), row["ip"]))


class PhoneGpsBridge:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._generation = 0
        self._starting = False
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._timer: threading.Timer | None = None
        self._tempdir: tempfile.TemporaryDirectory | None = None
        self._session: dict[str, Any] | None = None
        self._latest_fix: dict[str, Any] | None = None
        self._last_receive = 0.0
        self._state = "idle"
        self._error: str | None = None

    def start(self, bind_ip: str) -> dict:
        available = {row["ip"] for row in local_ipv4_interfaces()}
        try:
            normalized_ip = str(ipaddress.IPv4Address(bind_ip))
        except ipaddress.AddressValueError as exc:
            raise PhoneGpsError("Choose a valid local IPv4 interface.") from exc
        if normalized_ip not in available:
            raise PhoneGpsError("That address is no longer assigned to an active local interface. Refresh the list.", 409)
        with self._lock:
            if self._httpd is not None or self._starting:
                raise PhoneGpsError("A phone GPS session is already active.", 409)
            self._starting = True
            self._state = "starting"
            generation = self._generation

        temporary = tempfile.TemporaryDirectory(prefix="darksec-phone-gps-")
        cert_path = Path(temporary.name) / "server.crt"
        key_path = Path(temporary.name) / "server.key"
        httpd: ThreadingHTTPServer | None = None
        try:
            self._write_certificate(normalized_ip, cert_path, key_path)
            session_id = secrets.token_urlsafe(12)
            token = secrets.token_urlsafe(32)
            expires_epoch = time.time() + PAIR_SECONDS
            expires_at = datetime.fromtimestamp(expires_epoch, timezone.utc).isoformat(timespec="seconds")
            bridge = self

            class Handler(BaseHTTPRequestHandler):
                protocol_version = "HTTP/1.1"

                def log_message(self, _format: str, *_args: Any) -> None:
                    # Never log GPS payloads, the bearer token, or phone IPs.
                    return

                def _send(self, code: int, body: bytes, content_type: str) -> None:
                    self.send_response(code)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store, max-age=0")
                    self.send_header("Pragma", "no-cache")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Permissions-Policy", "geolocation=(self)")
                    self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    try:
                        self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError):
                        pass

                def _authorized(self, allow_query: bool = False) -> bool:
                    if self.headers.get("Host", "").lower() != f"{normalized_ip}:{httpd.server_address[1]}".lower():
                        return False
                    supplied = self.headers.get("Authorization", "")
                    if supplied.startswith("Bearer "):
                        supplied = supplied[7:]
                    elif allow_query:
                        supplied = parse_qs(urlsplit(self.path).query).get("t", [""])[0]
                    else:
                        supplied = ""
                    return secrets.compare_digest(str(supplied), token)

                def do_GET(self) -> None:
                    path = urlsplit(self.path).path
                    if path in ("/", "/index.html"):
                        if not self._authorized(allow_query=True):
                            self._send(404, b"Not found", "text/plain; charset=utf-8")
                            return
                        try:
                            page = PHONE_PAGE.read_bytes()
                        except OSError:
                            self._send(500, b"Phone GPS page unavailable", "text/plain; charset=utf-8")
                            return
                        self._send(200, page, "text/html; charset=utf-8")
                        return
                    if path == "/status":
                        if not self._authorized():
                            self._send(401, b"Unauthorized", "text/plain; charset=utf-8")
                            return
                        state = bridge.status()
                        body = json.dumps({"state": state["state"], "expires_at": expires_at}, separators=(",", ":")).encode()
                        self._send(200, body, "application/json; charset=utf-8")
                        return
                    self._send(404, b"Not found", "text/plain; charset=utf-8")

                def do_POST(self) -> None:
                    path = urlsplit(self.path).path
                    if path == "/stop" and self._authorized():
                        self._send(200, b"{\"ok\":true}", "application/json; charset=utf-8")
                        threading.Thread(target=bridge.stop, name="darksec-phone-gps-stop", daemon=True).start()
                        return
                    if path != "/fix" or not self._authorized():
                        self._send(404, b"Not found", "text/plain; charset=utf-8")
                        return
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                    except ValueError:
                        length = 0
                    if not 1 <= length <= MAX_BODY_BYTES:
                        self._send(413, b"Invalid payload size", "text/plain; charset=utf-8")
                        return
                    try:
                        payload = json.loads(self.rfile.read(length))
                        fix = bridge.receive(token, payload)
                    except PhoneGpsError as exc:
                        self._send(exc.status_code, str(exc).encode(), "text/plain; charset=utf-8")
                        return
                    except Exception:
                        self._send(400, b"Invalid GPS payload", "text/plain; charset=utf-8")
                        return
                    body = json.dumps({"ok": True, "received_at": fix["received_at"]}).encode()
                    self._send(200, body, "application/json; charset=utf-8")

            httpd = ThreadingHTTPServer((normalized_ip, 0), Handler)
            httpd.daemon_threads = True
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.minimum_version = ssl.TLSVersion.TLSv1_2
            tls.load_cert_chain(str(cert_path), str(key_path))
            httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
            url = f"https://{normalized_ip}:{httpd.server_address[1]}/?t={token}"
            thread = threading.Thread(target=httpd.serve_forever, name="darksec-phone-gps-https", daemon=True)
            with self._lock:
                if self._generation != generation or not self._starting:
                    raise PhoneGpsError("Phone GPS session was stopped before it could start.", 409)
                self._httpd = httpd
                self._thread = thread
                self._tempdir = temporary
                self._session = {
                    "id": session_id, "token": token, "ip": normalized_ip,
                    "port": httpd.server_address[1], "url": url,
                    "expires_epoch": expires_epoch, "expires_at": expires_at,
                }
                self._latest_fix = None
                self._last_receive = 0.0
                self._state = "waiting"
                self._error = None
                self._starting = False
                self._timer = threading.Timer(PAIR_SECONDS, self._expire)
                self._timer.daemon = True
                self._timer.start()
            thread.start()
            return {
                "session_id": session_id,
                "ip": normalized_ip,
                "port": httpd.server_address[1],
                "url": url,
                "expires_at": expires_at,
                "state": "waiting",
                "transport": "temporary local HTTPS",
            }
        except Exception:
            if httpd:
                try:
                    httpd.server_close()
                except Exception:
                    pass
            temporary.cleanup()
            with self._lock:
                if self._generation == generation:
                    self._starting = False
                    if self._state == "starting":
                        self._state = "error"
            raise

    def _write_certificate(self, ip: str, cert_path: Path, key_path: Path) -> None:
        now = datetime.now(timezone.utc)
        key = ec.generate_private_key(ec.SECP256R1())
        subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DarkSec temporary phone GPS")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(ip))]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256())
        )
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        os.chmod(cert_path, 0o600)
        os.chmod(key_path, 0o600)

    def qr_payload(self, session_id: str) -> str:
        with self._lock:
            session = self._session
            if not session or session["id"] != session_id or time.time() >= session["expires_epoch"]:
                raise PhoneGpsError("Phone GPS session expired; start a new one.", 410)
            return session["url"]

    def receive(self, token: str, payload: Any) -> dict:
        with self._lock:
            session = self._session
            if not session or time.time() >= session["expires_epoch"]:
                raise PhoneGpsError("Phone GPS session expired.", 410)
            if not secrets.compare_digest(str(token), session["token"]):
                raise PhoneGpsError("Unauthorized", 401)
            try:
                lat = float(payload["lat"])
                lon = float(payload["lon"])
                accuracy = float(payload["accuracy_m"])
                fix_epoch = float(payload["timestamp_ms"]) / 1000.0
            except (KeyError, TypeError, ValueError):
                raise PhoneGpsError("Invalid GPS fields.")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise PhoneGpsError("Coordinates outside valid bounds.")
            if not (math.isfinite(accuracy) and 0 <= accuracy <= MAX_HORIZONTAL_ACCURACY_M):
                raise PhoneGpsError("Invalid reported GPS accuracy.")
            now = time.time()
            if not (now - 120 <= fix_epoch <= now + 300):
                raise PhoneGpsError("Phone fix is stale or its clock is more than five minutes ahead.")
            if now - self._last_receive < MIN_FIX_INTERVAL_SECONDS:
                raise PhoneGpsError("GPS updates are arriving too quickly.", 429)
            fix = {
                "lat": lat, "lon": lon, "accuracy_m": accuracy,
                "alt_m": _finite(payload.get("alt_m")),
                "speed_mps": _finite(payload.get("speed_mps")),
                "heading_deg": _finite(payload.get("heading_deg")),
                "timestamp": datetime.fromtimestamp(fix_epoch, timezone.utc).isoformat(timespec="seconds"),
                "received_at": utcnow(), "received_epoch": now,
            }
            self._latest_fix = fix
            self._last_receive = now
            self._state = "receiving"
            self._error = None
            return fix

    def status(self) -> dict:
        with self._lock:
            expired = bool(self._session and time.time() >= self._session["expires_epoch"])
        if expired:
            self.stop()
            with self._lock:
                self._state = "expired"
        with self._lock:
            session = self._session
            fix = None
            if self._latest_fix:
                fix = {k: v for k, v in self._latest_fix.items() if k != "received_epoch"}
                fix["age_seconds"] = max(0, int(time.time() - float(self._latest_fix["received_epoch"])))
        return {
                "state": self._state,
                "session_id": session["id"] if session else None,
                "url": session["url"] if session else None,
                "expires_at": session["expires_at"] if session else None,
                "latest_fix": fix,
                "error": self._error,
                "transport": "temporary local HTTPS",
                "cloud_relay": False,
        }

    def _expire(self) -> None:
        self.stop()
        with self._lock:
            self._state = "expired"

    def accepted_fix(self) -> dict:
        with self._lock:
            if not self._session or time.time() >= self._session["expires_epoch"]:
                raise PhoneGpsError("Phone GPS session expired; request a fresh fix.", 410)
            fix = self._latest_fix
            if not fix:
                raise PhoneGpsError("No phone GPS fix received yet.", 409)
            if time.time() - float(fix["received_epoch"]) > FIX_FRESH_SECONDS:
                raise PhoneGpsError("Phone fix is older than 30 seconds; wait for a fresh fix.", 409)
            return {k: v for k, v in fix.items() if k != "received_epoch"}

    def stop(self) -> None:
        with self._lock:
            self._state = "stopped"
            self._generation += 1
            self._starting = False
            httpd, thread, timer, tempdir = self._httpd, self._thread, self._timer, self._tempdir
            self._cleanup_locked()
            self._latest_fix = None
            self._last_receive = 0.0
            self._error = None
        if timer and timer is not threading.current_thread():
            timer.cancel()
        if httpd:
            try:
                httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if thread and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=2)
        if tempdir:
            tempdir.cleanup()

    def _cleanup_locked(self) -> None:
        self._httpd = None
        self._thread = None
        self._timer = None
        self._tempdir = None
        self._session = None


def _finite(value: Any) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


pairing = PhoneGpsBridge()
