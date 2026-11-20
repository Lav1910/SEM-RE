"""Encrypts the one stored secret the Command Center keeps: the Google Business Profile refresh token (change 66).

AES-256-GCM with a key derived (HKDF-SHA256) from the app's SECRET_KEY, so the database alone never reveals the token.
If SECRET_KEY changes the token can no longer be read: the page then asks to connect again. Nothing is ever logged.
"""
import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

INFO = b"hbm-command-center-gbp-token-v1"


def _key():
    from webapp.app import app
    secret = app.config["SECRET_KEY"]
    secret = secret.encode() if isinstance(secret, str) else secret
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=INFO).derive(secret)


def seal(text):
    nonce = os.urandom(12)
    return base64.urlsafe_b64encode(nonce + AESGCM(_key()).encrypt(nonce, text.encode(), INFO)).decode()


def open_(blob):
    """-> the text, or None when it can't be decrypted (wrong key, damaged)."""
    try:
        raw = base64.urlsafe_b64decode(blob.encode())
        return AESGCM(_key()).decrypt(raw[:12], raw[12:], INFO).decode()
    except Exception:  # noqa: BLE001
        return None
