"""Command-line launcher for the DarkSec field console.

The server binds to 127.0.0.1 only. Electron starts it with --handshake and
reads the JSON line containing the port and launch token. For manual use:

    python3 -m darksec --open

The browser opens with a single-use bootstrap URL; the token is exchanged for
an HttpOnly cookie and removed from the address bar.
"""
from __future__ import annotations

import argparse
import secrets
import socket
import sys

import uvicorn

from . import __version__
from .config import APP_NAME
from .server import create_app
from . import tls


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="darksec", description="DarkSec NIGHTWATCH field console")
    parser.add_argument("--port", type=int, default=0, help="Port (0 chooses a free port)")
    parser.add_argument("--handshake", action="store_true", help="Print JSON handshake on startup")
    parser.add_argument("--token", default="", help="Use a fixed launch token (testing only)")
    parser.add_argument("--open", action="store_true", help="Open the default browser")
    parser.add_argument("--browser", action="store_true", help="Alias for --open")
    parser.add_argument("--no-index", action="store_true", help="Do not auto-start library indexing")
    parser.add_argument("--lan", action="store_true",
                        help="Expose the console on the local network (HTTPS, self-signed cert). "
                             "Default is loopback-only.")
    parser.add_argument("--lan-ip", default="", help="LAN IP to bind/advertise (auto-detected if omitted)")
    parser.add_argument("--log-level", default="warning", choices=["critical", "error", "warning", "info", "debug"])
    args = parser.parse_args(argv)

    port = args.port or free_port()
    token = args.token or secrets.token_urlsafe(32)

    lan = bool(args.lan)
    lan_ip = args.lan_ip.strip() if args.lan_ip else ""
    if lan and not lan_ip:
        lan_ip = tls.detect_lan_ip()
    bind_host = "0.0.0.0" if lan else "127.0.0.1"
    ssl_certfile = ssl_keyfile = None
    if lan:
        cert_path, key_path = tls.ensure_cert(lan_ip)
        ssl_certfile = str(cert_path)
        ssl_keyfile = str(key_path)

    app = create_app(port=port, token=token, open_browser=args.open or args.browser,
                     handshake=args.handshake, no_index=args.no_index,
                     lan=lan, lan_ip=lan_ip or None, secure_cookies=lan)
    scheme = "https" if lan else "http"
    host_for_url = lan_ip if lan else "127.0.0.1"
    url = f"{scheme}://{host_for_url}:{port}/?token={token}"
    if not args.handshake:
        print(f"{APP_NAME} {__version__}  {url}", flush=True)
        if lan:
            print(f"LAN ACCESS ENABLED on {lan_ip}:{port} (self-signed HTTPS). "
                  f"Accept the certificate warning on each client. OPSEC mode disables LAN access.", flush=True)
            try:
                import qrcode
                from qrcode.constants import ERROR_CORRECT_H
                qr = qrcode.QRCode(border=2, error_correction=ERROR_CORRECT_H)
                qr.add_data(url)
                qr.make(fit=True)
                print("Scan this QR code with the phone/tablet camera:", flush=True)
                qr.print_ascii(invert=True)
            except Exception as exc:
                print(f"(QR code unavailable: {exc})", flush=True)
    uvicorn_kwargs = dict(host=bind_host, port=port, log_level=args.log_level,
                          access_log=False, server_header=False)
    if ssl_certfile:
        uvicorn_kwargs.update(ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)
    uvicorn.run(app, **uvicorn_kwargs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
