"""Tests pour src.core.crypto (chiffrement Fernet)."""

import pytest

from src.core.crypto import _get_fernet, decrypt_str, encrypt_str


class TestCrypto:
    def test_encrypt_decrypt_roundtrip(self):
        plaintext = "ya29.fakeAccessToken123ABC"
        ct = encrypt_str(plaintext)
        assert isinstance(ct, bytes)
        assert ct != plaintext.encode()  # bien chiffré
        decrypted = decrypt_str(ct)
        assert decrypted == plaintext

    def test_encrypt_empty_raises(self):
        with pytest.raises(ValueError, match="vide"):
            encrypt_str("")

    def test_decrypt_invalid_raises(self):
        with pytest.raises(ValueError, match="invalide"):
            decrypt_str(b"this-is-not-fernet-ciphertext")

    def test_encrypt_unicode(self):
        plaintext = "secret avec accents éàç + emojis 🔐"
        ct = encrypt_str(plaintext)
        assert decrypt_str(ct) == plaintext

    def test_fernet_cached(self):
        """_get_fernet est lru_cached → même instance entre appels."""
        f1 = _get_fernet()
        f2 = _get_fernet()
        assert f1 is f2

    def test_encrypted_outputs_differ(self):
        """Fernet inclut un timestamp + IV → 2 chiffrements du même plaintext diffèrent."""
        ct1 = encrypt_str("same input")
        ct2 = encrypt_str("same input")
        assert ct1 != ct2  # IV différents
        # Mais déchiffrent au même résultat
        assert decrypt_str(ct1) == decrypt_str(ct2) == "same input"
