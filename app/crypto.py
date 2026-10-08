"""Encryption for Google refresh tokens at rest (Fernet)."""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from .config import get_config


class CryptoNotConfigured(RuntimeError):
    pass


def _fernet() -> Fernet:
    key = get_config().token_encryption_key
    if not key:
        raise CryptoNotConfigured("TOKEN_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise CryptoNotConfigured("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str | None:
    """Returns None if the value cannot be decrypted (for example the key was rotated)."""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken:
        return None
