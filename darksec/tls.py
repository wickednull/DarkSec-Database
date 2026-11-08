"""Self-signed TLS certificate generation for LAN mode.

DarkSec is local-only by default. When the operator explicitly enables LAN
access, the console is served over HTTPS with an auto-generated self-signed
certificate so the launch token and session cookie are never sent in clear
text. Browsers will show a warning the first time; that is expected for a
self-signed certificate on a private network.
"""
from __future__ import annotations

import datetime as _dt
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from . import config


def _lan_ip() -> str:
    """Best-effort detection of a non-loopback IPv4 address."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
    except Exception:
        pass
    try:
        import psutil
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if addr.family == socket.AF_INET and not addr.address.startswith("127."):
                    return addr.address
    except Exception:
        pass
    return "127.0.0.1"


def ensure_cert(host: str, cert_dir: Path | None = None) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating a self-signed cert if needed."""
    cert_dir = cert_dir or (config.DATA_DIR / "tls")
    cert_dir.mkdir(parents=True, exist_ok=True)
    cert_path = cert_dir / "darksec-lan.crt"
    key_path = cert_dir / "darksec-lan.key"

    # Reuse an existing, still-valid certificate for the same host.
    if cert_path.exists() and key_path.exists():
        try:
            cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
            if host in [san.value for san in cert.extensions.get_extension_for_class(
                    x509.SubjectAlternativeName).value] and cert.not_valid_after_utc > _dt.datetime.now(_dt.timezone.utc):
                return cert_path, key_path
        except Exception:
            pass

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, host),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DarkSec NIGHTWATCH"),
    ])
    alt_names = [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    try:
        alt_names.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        alt_names.append(x509.DNSName(host))

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(minutes=5))
        .not_valid_after(_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path, key_path


def detect_lan_ip() -> str:
    return _lan_ip()
