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
    parser.add_argument("--log-level", default="warning", choices=["critical", "error", "warning", "info", "debug"])
    args = parser.parse_args(argv)

    port = args.port or free_port()
    token = args.token or secrets.token_urlsafe(32)
    app = create_app(port=port, token=token, open_browser=args.open or args.browser,
                     handshake=args.handshake, no_index=args.no_index)
    if not args.handshake:
        print(f"{APP_NAME} {__version__}  http://127.0.0.1:{port}/?token={token}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level=args.log_level,
                access_log=False, server_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
