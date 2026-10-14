import pytest

from ai_newsdesk.secrets_box import SecretsBox, SecretsBoxError, derive_fernet_key

KEY_A = "a" * 64
KEY_B = "b" * 64
SECRET = "sk-test-" + "z" * 40


def test_roundtrip_and_ciphertext_hides_plaintext() -> None:
    box = SecretsBox(KEY_A)
    token = box.encrypt(SECRET)
    assert SECRET not in token
    assert box.decrypt(token) == SECRET
    assert box.encrypt(SECRET) != token  # Fernet uses a fresh IV each time


def test_key_derivation_is_deterministic_and_key_specific() -> None:
    assert derive_fernet_key(KEY_A) == derive_fernet_key(KEY_A)
    assert derive_fernet_key(KEY_A) != derive_fernet_key(KEY_B)
    assert len(derive_fernet_key(KEY_A)) == 44  # urlsafe base64 of 32 bytes


def test_wrong_key_raises_clean_error() -> None:
    token = SecretsBox(KEY_A).encrypt(SECRET)
    with pytest.raises(SecretsBoxError) as exc:
        SecretsBox(KEY_B).decrypt(token)
    assert token not in str(exc.value)
    assert exc.value.__suppress_context__ is True
    with pytest.raises(SecretsBoxError):
        SecretsBox(KEY_A).decrypt("not-a-token")


def test_repr_redacted_and_empty_key_refused() -> None:
    assert repr(SecretsBox(KEY_A)) == "SecretsBox(<redacted>)"
    assert KEY_A not in repr(SecretsBox(KEY_A))
    with pytest.raises(ValueError, match="SECRETS_KEY"):
        SecretsBox("")
