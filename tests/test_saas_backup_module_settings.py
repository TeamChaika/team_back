"""Backups must validate newly added private module credentials with their key."""

import json

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.saas_admin.company_module_settings import defaults
from app.saas_admin.postgres_backup import _validate_ciphertexts


def snapshot(value, cipher):
    return {
        "connections": [],
        "auth_provisioning": [],
        "company_module_settings": [
            {"ciphertext": cipher.encrypt(json.dumps(value).encode()).decode()}
        ],
    }


def test_module_settings_backup_checks_key_and_payload():
    key = Fernet.generate_key()
    cipher = Fernet(key)
    data = snapshot(defaults(), cipher)
    _validate_ciphertexts(data, key)
    with pytest.raises(InvalidToken):
        _validate_ciphertexts(data, Fernet.generate_key())
    with pytest.raises(ValueError, match="Invalid encrypted module settings"):
        _validate_ciphertexts(snapshot({"seller": None}, cipher), key)


def test_module_settings_backup_errors_never_expose_secret():
    key = Fernet.generate_key()
    values = defaults()
    secret = "invalid synthetic secret value"
    values["assistant"]["key"] = secret
    with pytest.raises(ValueError) as caught:
        _validate_ciphertexts(snapshot(values, Fernet(key)), key)
    assert str(caught.value) == "Invalid encrypted module settings"
    assert secret not in str(caught.value)


@pytest.mark.parametrize("group", ["telegram", "assistant", "seller"])
def test_module_settings_backup_rejects_incomplete_persisted_group(group):
    key = Fernet.generate_key()
    values = defaults()
    values[group] = {}
    with pytest.raises(ValueError, match="Invalid encrypted module settings"):
        _validate_ciphertexts(snapshot(values, Fernet(key)), key)
