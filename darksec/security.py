"""Vault encryption and operator-data purge.

Notes are encrypted with AES-256-GCM using a key derived from the operator
passphrase (PBKDF2-HMAC-SHA256, 600k iterations). The key lives only in RAM
and is dropped on lock, timeout, or shutdown.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from . import config

VERIFIER = b"DARKSEC-VAULT-V1"
KDF_ITERATIONS = 600_000


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def _unb64(data: str) -> bytes:
    return base64.urlsafe_b64decode(data.encode())


class Vault:
    def __init__(self, db):
        self.db = db
        self._key: bytes | None = None
        self._unlocked_at: float | None = None

    # ------------------------------------------------------------------ state
    def configured(self) -> bool:
        return bool(self.db.get_setting("vault_check"))

    def unlocked(self) -> bool:
        if not self._key:
            return False
        timeout = float(self.db.get_setting("vault_timeout_min", 15) or 15)
        if timeout > 0 and self._unlocked_at and time.time() - self._unlocked_at > timeout * 60:
            self.lock()
            return False
        return True

    def status(self) -> dict:
        return {"configured": self.configured(), "unlocked": self.unlocked(),
                "timeout_min": self.db.get_setting("vault_timeout_min", 15)}

    def _derive(self, passphrase: str, salt: bytes) -> bytes:
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                         iterations=KDF_ITERATIONS)
        return kdf.derive(passphrase.encode("utf-8"))

    # --------------------------------------------------------------- lifecycle
    def setup(self, passphrase: str) -> dict:
        if len(passphrase) < 8:
            raise ValueError("Passphrase must be at least 8 characters")
        if self.configured():
            raise ValueError("Vault is already configured")
        salt = os.urandom(16)
        key = self._derive(passphrase, salt)
        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, VERIFIER, b"darksec-vault")
        self.db.set_setting("vault_salt", _b64(salt))
        self.db.set_setting("vault_check", _b64(nonce + ct))
        self.db.set_setting("vault_kdf", f"pbkdf2-sha256-{KDF_ITERATIONS}")
        self._key = key
        self._unlocked_at = time.time()
        self.db.audit("VAULT_SETUP", "Encrypted vault created")
        return self.status()

    def unlock(self, passphrase: str) -> dict:
        if not self.configured():
            raise ValueError("Vault is not configured")
        salt = _unb64(self.db.get_setting("vault_salt"))
        raw = _unb64(self.db.get_setting("vault_check"))
        key = self._derive(passphrase, salt)
        try:
            AESGCM(key).decrypt(raw[:12], raw[12:], b"darksec-vault")
        except InvalidTag:
            self.db.audit("VAULT_UNLOCK_FAIL", "Bad passphrase")
            raise ValueError("Invalid passphrase")
        self._key = key
        self._unlocked_at = time.time()
        self.db.audit("VAULT_UNLOCK", "Vault unlocked")
        return self.status()

    def lock(self) -> dict:
        self._key = None
        self._unlocked_at = None
        return self.status()

    def change_passphrase(self, old: str, new: str) -> dict:
        self.unlock(old)
        if len(new) < 8:
            raise ValueError("New passphrase must be at least 8 characters")
        salt = os.urandom(16)
        key = self._derive(new, salt)

        # Re-encrypt every note under the new key before the verifier is
        # replaced. If any note cannot be decrypted the whole change is
        # aborted, so an operator can never rekey themselves out of their data.
        rows = self.db.query("SELECT id,cipher FROM notes")
        reencrypted: list[tuple[str, int]] = []
        for row in rows:
            try:
                plaintext = self.decrypt(row["cipher"])
            except Exception as exc:
                raise ValueError(
                    f"Passphrase change aborted: note #{row['id']} could not be decrypted ({exc})"
                ) from exc
            nonce = os.urandom(12)
            ct = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), b"darksec-note")
            reencrypted.append(("v1." + _b64(nonce) + "." + _b64(ct), row["id"]))

        nonce = os.urandom(12)
        ct = AESGCM(key).encrypt(nonce, VERIFIER, b"darksec-vault")
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("vault_salt", json.dumps(_b64(salt))),
            )
            conn.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("vault_check", json.dumps(_b64(nonce + ct))),
            )
            for cipher, note_id in reencrypted:
                conn.execute("UPDATE notes SET cipher=? WHERE id=?", (cipher, note_id))
        self._key = key
        self._unlocked_at = time.time()
        self.db.audit("VAULT_PASSCHANGE", f"Vault passphrase changed; notes rekeyed={len(reencrypted)}")
        return self.status()

    # ------------------------------------------------------------------ crypto
    def encrypt(self, text: str) -> str:
        if not self.unlocked():
            raise PermissionError("Vault is locked")
        nonce = os.urandom(12)
        ct = AESGCM(self._key).encrypt(nonce, text.encode("utf-8"), b"darksec-note")
        return "v1." + _b64(nonce) + "." + _b64(ct)

    def decrypt(self, blob: str) -> str:
        if not self.unlocked():
            raise PermissionError("Vault is locked")
        try:
            version, nonce_b64, ct_b64 = blob.split(".", 2)
            if version != "v1":
                raise ValueError("Unsupported ciphertext version")
            return AESGCM(self._key).decrypt(_unb64(nonce_b64), _unb64(ct_b64), b"darksec-note").decode("utf-8")
        except InvalidTag:
            raise ValueError("Ciphertext authentication failed")

    # ------------------------------------------------------------------- purge
    def purge_operator_data(self) -> dict:
        """Delete all operator-entered data, the vault, uploads and recordings.

        The document library and its index are deliberately preserved so the
        kit remains useful after a compromise.
        """
        tables = [
            "audit", "log_entries", "tasks",
            "patients", "vitals", "medication_log", "weather_log", "water_log", "power_log", "rad_log",
            "waypoints", "tracks", "radio_log", "notes", "incidents", "inventory",
            "caches", "comms_plans", "opords", "trip_plans", "sdr_jobs", "sdr_recordings",
            "checklist_state",
        ]
        for table in tables:
            self.db.execute(f"DELETE FROM {table}")
        # Feed definitions are part of the baseline kit, not operator data.
        # Reset their state and cached intel instead of deleting them, so the
        # console stays usable immediately after a purge.
        self.db.execute("UPDATE feeds SET status='NEVER', fetched_at=NULL, payload=NULL, error=NULL")
        for key in ("vault_salt", "vault_check", "vault_kdf"):
            self.db.execute("DELETE FROM settings WHERE key=?", (key,))
        for directory in (config.UPLOAD_DIR, config.RECORDING_DIR):
            for child in Path(directory).iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except Exception:
                    pass
        self.lock()
        self.db.audit("PANIC_PURGE", "Operator data purged; library preserved")
        return {"purged": True, "library_preserved": True}

    def purge_everything(self) -> dict:
        """Full destructive reset: operator data and the library index.

        PDF files themselves are never deleted by the application.
        """
        result = self.purge_operator_data()
        # Keep the meta/seed markers: feed definitions are baseline kit data.
        # Deleting meta would make the next start seed a second copy of them.
        for table in ("library_fts", "library_docs"):
            try:
                self.db.execute(f"DELETE FROM {table}")
            except Exception:
                pass
        self.db.audit("FULL_PURGE", "Index and operator data purged; PDF files preserved")
        return {**result, "index_preserved": False}
