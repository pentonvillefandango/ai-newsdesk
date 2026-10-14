"""Encrypt API keys saved on the Settings page (V1-S1).

The Fernet key is derived from SECRETS_KEY with HKDF-SHA256. SECRETS_KEY is already a
high-entropy random secret (at least 32 characters, `openssl rand -hex 32`), so a slow
password KDF such as PBKDF2 would add start-up cost and no security; HKDF is the standard
extractor for key material like this. The `info` label separates this key from any other
use of the same secret, and versions it.
"""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

HKDF_INFO = b"ai-newsdesk/secrets-box/v1"


class SecretsBoxError(Exception):
    """A token could not be decrypted (wrong SECRETS_KEY or corrupted). Never holds data."""


def derive_fernet_key(secrets_key: str) -> bytes:
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=HKDF_INFO)
    return base64.urlsafe_b64encode(hkdf.derive(secrets_key.encode()))


class SecretsBox:
    def __init__(self, secrets_key: str) -> None:
        if not secrets_key:
            raise ValueError("SecretsBox needs a non-empty SECRETS_KEY")
        self._fernet = Fernet(derive_fernet_key(secrets_key))

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        try:
            return self._fernet.decrypt(token.encode()).decode()
        except (InvalidToken, ValueError):
            # `from None`: the cause would carry the token into tracebacks and logs.
            raise SecretsBoxError(
                "stored secret could not be decrypted (was SECRETS_KEY changed?)"
            ) from None

    def __repr__(self) -> str:
        return "SecretsBox(<redacted>)"
