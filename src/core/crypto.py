"""Chiffrement symétrique pour les secrets stockés en DB.

Utilise Fernet (AES-128-CBC + HMAC-SHA256) avec une clé dérivée du `secret_key`
applicatif. Permet de chiffrer/déchiffrer les tokens OAuth, mots de passe Garmin, etc.

Si Marc rotate son `secret_key`, tous les tokens chiffrés deviennent illisibles.
Dans ce cas il faudra re-déclencher les flows OAuth (acceptable car secret_key
ne change pas en pratique).
"""

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from src.core.config import get_settings


@lru_cache
def _get_fernet() -> Fernet:
    """Dérive une clé Fernet depuis le secret_key applicatif (SHA-256 → base64).

    Cached : un seul Fernet par process. Si secret_key change, app restart requis.
    """
    secret = get_settings().secret_key.encode("utf-8")
    if len(secret) < 8:
        raise ValueError("secret_key trop court (minimum 8 chars)")
    # SHA-256 = 32 bytes → exactement la taille attendue par Fernet (base64-encoded)
    digest = hashlib.sha256(secret).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_str(plaintext: str) -> bytes:
    """Chiffre une string en bytes (à stocker en DB)."""
    if not plaintext:
        raise ValueError("plaintext vide")
    return _get_fernet().encrypt(plaintext.encode("utf-8"))


def decrypt_str(ciphertext: bytes) -> str:
    """Déchiffre des bytes en string. Lève InvalidToken si mauvaise clé/corruption."""
    try:
        return _get_fernet().decrypt(ciphertext).decode("utf-8")
    except InvalidToken as e:
        raise ValueError("Token chiffré invalide ou clé incorrecte") from e
