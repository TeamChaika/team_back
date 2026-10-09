from types import SimpleNamespace

import pytest

from app.saas_admin.runtime_module_settings import apply_company_settings


def test_central_empty_values_remove_old_file_secrets_without_fallback():
    configuration = {
        "document_settings": {
            "worker_enabled": True,
            "bot_token": "old-secret",
            "bot_username": "old_bot",
            "commercial_seller_json": {"name": "Old seller"},
        },
        "assistant_settings": {
            "provider": "timeweb",
            "api_key": "old-ai",
            "timeweb_agent_id": "old-agent",
            "model": "old-model",
            "max_output_tokens": 100,
        },
    }
    calls = []

    def settings(identifier, *, expected_version):
        calls.append((identifier, expected_version))
        return {
            "document_settings": {
                "bot_token": "",
                "bot_username": "",
                "commercial_seller_json": "",
            },
            "assistant_settings": {"provider": "openai", "api_key": "", "model": "central-model"},
        }

    result = apply_company_settings(
        configuration, {"id": "company-a", "version": 9}, SimpleNamespace(runtime_settings=settings)
    )
    assert calls == [("company-a", 9)]
    assert result["document_settings"] == {
        "worker_enabled": True,
        "bot_token": "",
        "bot_username": "",
        "commercial_seller_json": "",
    }
    assert result["assistant_settings"] == {
        "max_output_tokens": 100,
        "timeweb_agent_id": "",
        "provider": "openai",
        "api_key": "",
        "model": "central-model",
    }
    assert "old-secret" not in str(result) and "old-ai" not in str(result)


def test_unlisted_customer_settings_cannot_enter_manifest():
    service = SimpleNamespace(
        runtime_settings=lambda *args, **kwargs: {
            "document_settings": {"admin_key": "blocked"},
            "assistant_settings": {},
        }
    )
    with pytest.raises(ValueError, match="Unknown central"):
        apply_company_settings({}, {"id": "a", "version": 1}, service)
    with pytest.raises(ValueError, match="Unknown tenant"):
        apply_company_settings(
            {"assistant_settings": {"base_url": "https://foreign"}},
            {"id": "a", "version": 1},
            service,
        )
