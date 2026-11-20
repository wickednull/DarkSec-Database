"""Detect an ASGI WebSocket backend for uvicorn.

FastAPI's WebSocket routes need a real ASGI WebSocket implementation; uvicorn
imports one from ``websockets`` or ``wsproto``. Without either, every upgrade
request is rejected with "Unsupported upgrade request" and the live SDR panel
cannot stream audio or spectrum. The rest of the console works normally, so
this is reported once at startup and in the SDR device inventory instead of
letting uvicorn repeat a warning for every connection attempt.
"""
from __future__ import annotations

OPTIONAL_LIBS = ("websockets", "wsproto")


def _version(module) -> str:
    return str(getattr(module, "__version__", "") or "")


def detect() -> dict:
    """Return which ASGI WebSocket implementation is importable."""
    for name in OPTIONAL_LIBS:
        try:
            module = __import__(name)
        except Exception:
            continue
        return {"available": True, "library": name, "version": _version(module)}
    return {
        "available": False,
        "library": "",
        "version": "",
        "install": "pip install websockets",
        "hint": ("Live SDR audio needs a WebSocket backend. Install it with "
                 "`pip install websockets` (or `pip install 'uvicorn[standard]'`) "
                 "using the same Python that runs DarkSec, then restart."),
    }


def silence_uvicorn_ws_warnings() -> None:
    """Drop uvicorn's per-request WebSocket warnings when there is no backend.

    uvicorn logs "Unsupported upgrade request" and "No supported WebSocket
    library detected" for every upgrade attempt when neither backend exists,
    which floods the console of an otherwise working install. The startup note
    and the SDR panel explain the missing dependency once instead.
    """
    import logging

    messages = ("Unsupported upgrade request",
                "No supported WebSocket library detected")

    class _DropWsNoise(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            text = record.getMessage()
            return not any(text.startswith(prefix) for prefix in messages)

    logger = logging.getLogger("uvicorn.error")
    if not any(isinstance(f, _DropWsNoise) for f in logger.filters):
        logger.addFilter(_DropWsNoise())


def uvicorn_ws_mode() -> str:
    """Return uvicorn's ``ws`` setting and quiet the no-backend warning flood."""
    if detect()["available"]:
        return "auto"
    silence_uvicorn_ws_warnings()
    return "none"
