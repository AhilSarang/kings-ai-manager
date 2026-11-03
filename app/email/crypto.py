"""Encryption at rest for the Gmail refresh token (Fernet, key from TOKEN_ENCRYPTION_KEY)."""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class TokenCryptoError(Exception):
    pass


def _fernet() -> Fernet:
    key = get_settings().token_encryption_key.get_secret_value()
    if not key:
        raise TokenCryptoError("TOKEN_ENCRYPTION_KEY is not set")
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise TokenCryptoError("TOKEN_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise TokenCryptoError("Stored token can't be decrypted with the current key") from exc


def new_key() -> str:
    return Fernet.generate_key().decode()
