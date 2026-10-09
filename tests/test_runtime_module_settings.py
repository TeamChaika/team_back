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


@pytest.mark.parametrize(
    "central,configured",
    [
        ({"provider": "openai", "api_key": "", "model": "own-model"}, False),
        ({"provider": "openai", "api_key": "own-test-key", "model": "own-model"}, True),
        ({"provider": "timeweb", "api_key": "own-test-key", "model": "own-model"}, False),
        (
            {
                "provider": "timeweb",
                "api_key": "own-test-key",
                "model": "own-model",
                "timeweb_agent_id": "11111111-1111-4111-8111-111111111111",
            },
            True,
        ),
    ],
)
def test_authoritative_ai_settings_load_without_legacy_credentials(
    monkeypatch, central, configured
):
    from app.web import assistant

    monkeypatch.setattr(assistant, "load_runtime", lambda: SimpleNamespace(mode="tenant"))
    monkeypatch.setenv("CHAIKA_AI_API_KEY", "foreign-test-key")
    monkeypatch.setenv("CHAIKA_AI_TIMEWEB_AGENT_ID", "22222222-2222-4222-8222-222222222222")
    service = SimpleNamespace(
        runtime_settings=lambda *args, **kwargs: {
            "document_settings": {},
            "assistant_settings": central,
        }
    )
    result = apply_company_settings(
        {"assistant_settings": {"api_key": "stale-test-key", "timeweb_agent_id": "old-agent"}},
        {"id": "own-company", "version": 1},
        service,
    )["assistant_settings"]
    # Exercise the same strings consumed by AssistantSettings in the child process.
    for name, value in result.items():
        monkeypatch.setenv("RESTCONTROL_TENANT_AI_" + name.upper(), str(value))
    settings = assistant.AssistantSettings(_env_file=None)
    assert settings.configured is configured
    assert settings.api_key.get_secret_value() == central["api_key"]
    assert str(settings.timeweb_agent_id) == str(central.get("timeweb_agent_id"))


@pytest.mark.parametrize("blank", [None, "", "  "])
def test_optional_blank_agent_id_is_omitted_before_environment_serialization(blank):
    from app.web.assistant import AssistantSettings

    service = SimpleNamespace(
        runtime_settings=lambda *args, **kwargs: {
            "document_settings": {},
            "assistant_settings": {"provider": "timeweb", "api_key": "", "timeweb_agent_id": blank},
        }
    )
    result = apply_company_settings({}, {"id": "own", "version": 1}, service)["assistant_settings"]
    assert "timeweb_agent_id" not in result
    settings = AssistantSettings(_env_file=None, **result)
    assert settings.timeweb_agent_id is None
    assert settings.configured is False


def test_nonblank_invalid_agent_id_still_fails_typed_validation():
    from pydantic import ValidationError

    from app.web.assistant import AssistantSettings

    service = SimpleNamespace(
        runtime_settings=lambda *args, **kwargs: {
            "document_settings": {},
            "assistant_settings": {
                "provider": "timeweb",
                "api_key": "",
                "timeweb_agent_id": "invalid",
            },
        }
    )
    result = apply_company_settings({}, {"id": "own", "version": 1}, service)["assistant_settings"]
    with pytest.raises(ValidationError):
        AssistantSettings(_env_file=None, **result)
